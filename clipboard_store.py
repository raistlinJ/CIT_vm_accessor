"""Durable per-VM clipboard accounting, shared by sessions and workers."""
from contextlib import contextmanager
import os
import sqlite3
import time

from clipboard_policy import DIRECTIONS


class ClipboardStore:
    def __init__(self, directory):
        os.makedirs(directory, mode=0o700, exist_ok=True)
        self.path = os.path.join(directory, 'clipboard_usage.sqlite3')
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS usage (
                cluster TEXT NOT NULL, vmid INTEGER NOT NULL, direction TEXT NOT NULL,
                epoch TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0,
                denied_at REAL, PRIMARY KEY(cluster,vmid,direction,epoch))''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def snapshot(self, cluster, vmid, policy):
        with self.connect() as db:
            result = {}
            for direction in DIRECTIONS:
                rule = policy[direction]
                row = db.execute('SELECT used,denied_at FROM usage WHERE cluster=? AND vmid=? AND direction=? AND epoch=?',
                                 (rule.get('scope') or cluster, vmid, direction, rule['reset'])).fetchone()
                used = row['used'] if row else 0
                limit = rule['limit']
                result[direction] = {**rule, 'used': used,
                    'remaining': None if limit is None else max(0, limit-used),
                    'denied_at': row['denied_at'] if row else None}
            return result

    def consume(self, cluster, vmid, policy, events):
        """Authorize a batch atomically; rejected transfers consume no allowance."""
        amounts = {direction: 0 for direction in DIRECTIONS}
        for direction, count in events:
            if direction not in amounts or type(count) is not int or count < 0:
                return False
            if not policy[direction]['enabled']:
                return False
            amounts[direction] += count
        with self.connect() as db:
            for direction, count in amounts.items():
                rule = policy[direction]
                db.execute('INSERT OR IGNORE INTO usage(cluster,vmid,direction,epoch) VALUES(?,?,?,?)',
                           (rule.get('scope') or cluster, vmid, direction, rule['reset']))
                used = db.execute('SELECT used FROM usage WHERE cluster=? AND vmid=? AND direction=? AND epoch=?',
                                  (rule.get('scope') or cluster, vmid, direction, rule['reset'])).fetchone()['used']
                if count and rule['limit'] is not None and used + count > rule['limit']:
                    db.execute('UPDATE usage SET denied_at=? WHERE cluster=? AND vmid=? AND direction=? AND epoch=?',
                               (time.time(), rule.get('scope') or cluster, vmid, direction, rule['reset']))
                    return False
            for direction, count in amounts.items():
                db.execute('UPDATE usage SET used=used+? WHERE cluster=? AND vmid=? AND direction=? AND epoch=?',
                           (count, policy[direction].get('scope') or cluster, vmid, direction, policy[direction]['reset']))
        return True

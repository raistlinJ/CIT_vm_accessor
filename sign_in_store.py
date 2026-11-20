"""Persistent browser sign-ins and activity for the latest session per account."""

from contextlib import closing
from pathlib import Path
import sqlite3


class SignInStore:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = directory / 'sign_ins.sqlite3'
        with closing(self._connect()) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sign_in_events (
                    id INTEGER PRIMARY KEY, cluster TEXT NOT NULL,
                    username TEXT NOT NULL, timestamp REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS latest_sign_ins (
                    cluster TEXT NOT NULL, username TEXT NOT NULL,
                    timestamp REAL NOT NULL, PRIMARY KEY (cluster, username));
                CREATE TABLE IF NOT EXISTS latest_sign_in_activity (
                    cluster TEXT NOT NULL, username TEXT NOT NULL, signed_in_at REAL NOT NULL,
                    session_id TEXT NOT NULL UNIQUE, seen_at REAL NOT NULL, signed_out_at REAL,
                    PRIMARY KEY (cluster, username));
                CREATE TABLE IF NOT EXISTS sign_in_presence (
                    session_id TEXT PRIMARY KEY, cluster TEXT NOT NULL,
                    username TEXT NOT NULL, seen_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS sign_in_presence_account ON sign_in_presence (cluster, username, seen_at);
            ''')

    def _connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def record(self, cluster, username, timestamp, max_events, *, session_id=None):
        with closing(self._connect()) as db, db:
            db.execute('''INSERT INTO latest_sign_ins (cluster, username, timestamp) VALUES (?, ?, ?)
                ON CONFLICT (cluster, username) DO UPDATE SET timestamp = max(timestamp, excluded.timestamp)''',
                (cluster, username, timestamp))
            db.execute('INSERT INTO sign_in_events (cluster, username, timestamp) VALUES (?, ?, ?)',
                       (cluster, username, timestamp))
            db.execute('''DELETE FROM sign_in_events WHERE id <= (
                SELECT id FROM sign_in_events ORDER BY id DESC LIMIT 1 OFFSET ?)''', (max_events,))
            if session_id:
                self._insert_presence(db, cluster, username, timestamp, session_id)
                db.execute('''INSERT INTO latest_sign_in_activity
                    (cluster, username, signed_in_at, session_id, seen_at)
                    SELECT cluster, username, timestamp, ?, timestamp FROM latest_sign_ins
                    WHERE cluster = ? AND username = ? AND timestamp = ?
                    ON CONFLICT (cluster, username) DO UPDATE SET
                        signed_in_at = excluded.signed_in_at, session_id = excluded.session_id,
                        seen_at = excluded.seen_at, signed_out_at = NULL''',
                           (session_id, cluster, username, timestamp))

    def start_presence(self, cluster, username, timestamp, session_id):
        with closing(self._connect()) as db, db:
            self._insert_presence(db, cluster, username, timestamp, session_id)

    @staticmethod
    def _insert_presence(db, cluster, username, timestamp, session_id):
        db.execute('DELETE FROM sign_in_presence WHERE seen_at < ?', (timestamp - 31 * 86400,))
        db.execute('INSERT INTO sign_in_presence (session_id, cluster, username, seen_at) VALUES (?, ?, ?, ?)',
                   (session_id, cluster, username, timestamp))

    def touch_presence(self, session_id, timestamp):
        with closing(self._connect()) as db, db:
            updated = db.execute('UPDATE sign_in_presence SET seen_at = max(seen_at, ?) WHERE session_id = ?',
                                 (timestamp, session_id))
            if updated.rowcount:
                db.execute('UPDATE latest_sign_in_activity SET seen_at = max(seen_at, ?) WHERE session_id = ?',
                           (timestamp, session_id))
            return updated.rowcount > 0

    def end_presence(self, session_id, *, signed_out_at=None):
        with closing(self._connect()) as db, db:
            if signed_out_at is not None:
                db.execute('''UPDATE latest_sign_in_activity SET signed_out_at = max(seen_at, ?)
                    WHERE session_id = ? AND signed_out_at IS NULL''', (signed_out_at, session_id))
            db.execute('DELETE FROM sign_in_presence WHERE session_id = ?', (session_id,))

    def active_accounts(self, cluster, usernames, observed_at, ttl):
        active = set()
        with closing(self._connect()) as db:
            for offset in range(0, len(usernames), 500):
                batch = usernames[offset:offset + 500]
                placeholders = ','.join('?' for _ in batch)
                rows = db.execute('SELECT DISTINCT username FROM sign_in_presence '
                                  f'WHERE cluster = ? AND seen_at > ? AND seen_at <= ? AND username IN ({placeholders})',
                                  (cluster, observed_at - ttl, observed_at, *batch))
                active.update(username for username, in rows)
        return sorted(active)

    def latest(self, cluster, usernames):
        return {username: activity[0] for username, activity in self.latest_activity(cluster, usernames).items()}

    def latest_activity(self, cluster, usernames):
        result = {}
        with closing(self._connect()) as db:
            # Keep the parameter count below SQLite's older default limit.
            for offset in range(0, len(usernames), 500):
                batch = usernames[offset:offset + 500]
                placeholders = ','.join('?' for _ in batch)
                rows = db.execute('''SELECT l.username, l.timestamp, a.seen_at, a.signed_out_at
                    FROM latest_sign_ins l LEFT JOIN latest_sign_in_activity a
                    ON a.cluster = l.cluster AND a.username = l.username AND a.signed_in_at = l.timestamp
                    ''' + f'WHERE l.cluster = ? AND l.username IN ({placeholders})', (cluster, *batch))
                result.update({username: (start, seen, end) for username, start, seen, end in rows})
        return result

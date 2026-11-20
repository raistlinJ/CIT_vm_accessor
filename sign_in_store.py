"""Persistent browser sign-ins and their latest timestamp per account."""

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
            ''')

    def _connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def record(self, cluster, username, timestamp, max_events):
        with closing(self._connect()) as db, db:
            db.execute('''INSERT INTO latest_sign_ins (cluster, username, timestamp) VALUES (?, ?, ?)
                ON CONFLICT (cluster, username) DO UPDATE SET timestamp = max(timestamp, excluded.timestamp)''',
                (cluster, username, timestamp))
            db.execute('INSERT INTO sign_in_events (cluster, username, timestamp) VALUES (?, ?, ?)',
                       (cluster, username, timestamp))
            db.execute('''DELETE FROM sign_in_events WHERE id <= (
                SELECT id FROM sign_in_events ORDER BY id DESC LIMIT 1 OFFSET ?)''', (max_events,))

    def latest(self, cluster, usernames):
        result = {}
        with closing(self._connect()) as db:
            # Keep the parameter count below SQLite's older default limit.
            for offset in range(0, len(usernames), 500):
                batch = usernames[offset:offset + 500]
                placeholders = ','.join('?' for _ in batch)
                rows = db.execute('SELECT username, timestamp FROM latest_sign_ins '
                                  f'WHERE cluster = ? AND username IN ({placeholders})', (cluster, *batch))
                result.update(rows)
        return result

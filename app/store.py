from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import uuid


def now_ts():
    return int(datetime.now(timezone.utc).timestamp())


def new_id():
    return uuid.uuid4().hex


class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        (root / 'contents').mkdir(exist_ok=True)
        with self.connection() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS contents (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, kind TEXT NOT NULL,
                    created_at INTEGER NOT NULL, last_displayed INTEGER, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    content_id TEXT NOT NULL REFERENCES contents(id) ON DELETE RESTRICT,
                    kind TEXT NOT NULL, run_at INTEGER, cron TEXT, enabled INTEGER NOT NULL,
                    next_run INTEGER, created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, content_id TEXT REFERENCES contents(id) ON DELETE SET NULL,
                    title TEXT NOT NULL, schedule_id TEXT REFERENCES schedules(id) ON DELETE SET NULL,
                    scheduled_for INTEGER, status TEXT NOT NULL, error TEXT,
                    created_at INTEGER NOT NULL, finished_at INTEGER,
                    UNIQUE(schedule_id, scheduled_for)
                );
                CREATE TABLE IF NOT EXISTS display_state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), content_id TEXT,
                    title TEXT, updated_at INTEGER, snapshot TEXT, status TEXT NOT NULL, error TEXT
                );
                INSERT OR IGNORE INTO display_state(singleton, status) VALUES(1, 'unknown');
                CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at);
                CREATE INDEX IF NOT EXISTS schedules_due ON schedules(enabled, next_run);
            ''')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.root / 'controller.sqlite3', timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def rows(self, query, args=()):
        with self.connection() as db:
            return [dict(row) for row in db.execute(query, args)]

    def one(self, query, args=()):
        rows = self.rows(query, args)
        return rows[0] if rows else None

    def image_path(self, content_id):
        # Caller only uses UUIDs returned from database lookups.
        return self.root / 'contents' / f'{content_id}.png'

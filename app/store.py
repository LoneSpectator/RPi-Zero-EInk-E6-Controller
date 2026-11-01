from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import uuid

MAX_LOG_RETENTION_DAYS = 36500
LOG_TIME = 'COALESCE(finished_at,created_at)'


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
                CREATE TABLE IF NOT EXISTS battery_learning (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS log_settings (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    retention_days INTEGER NOT NULL CHECK(retention_days >= 1)
                );
                INSERT OR IGNORE INTO log_settings VALUES(1,7);
                CREATE INDEX IF NOT EXISTS jobs_record_time ON jobs(COALESCE(finished_at,created_at));
            ''')
            # Additive migration for existing deployments and saved screen images.
            for table, columns in {
                'jobs': {'source_snapshot': 'TEXT', 'purpose': "TEXT NOT NULL DEFAULT 'display'"},
                'display_state': {'source_snapshot': 'TEXT', 'battery_warning': 'INTEGER NOT NULL DEFAULT 0'},
            }.items():
                existing = {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}
                for name, declaration in columns.items():
                    if name not in existing:
                        db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')

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

    @staticmethod
    def _prune_jobs(db, now, days):
        cutoff = now - days * 86400
        return db.execute(f"DELETE FROM jobs WHERE status IN ('done','failed') AND {LOG_TIME} < ?", (cutoff,)).rowcount

    def prune_jobs(self, now=None):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            days = db.execute('SELECT retention_days FROM log_settings WHERE singleton=1').fetchone()[0]
            return self._prune_jobs(db, now_ts() if now is None else now, days)

    def log_settings(self):
        return self.one('SELECT retention_days FROM log_settings WHERE singleton=1')

    def update_log_settings(self, days):
        if type(days) is not int or not 1 <= days <= MAX_LOG_RETENTION_DAYS:
            raise ValueError(f'保留天数需要是 1–{MAX_LOG_RETENTION_DAYS} 的整数')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE log_settings SET retention_days=? WHERE singleton=1', (days,))
            self._prune_jobs(db, now_ts(), days)
        return {'retention_days': days}

    def job_history(self, page=1, page_size=20, recent=False):
        with self.connection() as db:
            db.execute('BEGIN')
            days = db.execute('SELECT retention_days FROM log_settings WHERE singleton=1').fetchone()[0]
            cutoff = now_ts() - days * 86400
            visible = f"(status IN ('pending','running') OR {LOG_TIME} >= ?)"
            if recent:
                return [dict(row) for row in db.execute(
                    f'SELECT * FROM jobs WHERE {visible} ORDER BY {LOG_TIME} DESC,rowid DESC LIMIT 5', (cutoff,))]
            total = db.execute(f'SELECT COUNT(*) FROM jobs WHERE {visible}', (cutoff,)).fetchone()[0]
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, pages)
            items = [dict(row) for row in db.execute(
                f'SELECT * FROM jobs WHERE {visible} ORDER BY {LOG_TIME} DESC,rowid DESC LIMIT ? OFFSET ?',
                (cutoff, page_size, (page - 1) * page_size))]
            return {'items': items, 'total': total, 'page': page, 'page_size': page_size,
                    'total_pages': pages, 'retention_days': days}

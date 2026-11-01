"""Durable serial display queue and minute-resolution scheduler (single process)."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import logging
import threading
from croniter import croniter
from PIL import Image
from .store import now_ts, new_id
from .hardware import MockDisplay, WaveshareDisplay

log = logging.getLogger(__name__)
GRACE_SECONDS = 120
MAX_PENDING = 20


class DisplayBusyError(Exception):
    """A manual refresh cannot be accepted until the display queue is idle."""


def next_cron(expression, after, tz):
    if not expression or len(expression.split()) != 5:
        raise ValueError('cron 必须恰好为五字段：分 时 日 月 周')
    # Reject @aliases, seconds, year fields and rare expressions before saving.
    try:
        base = datetime.fromtimestamp(after, ZoneInfo(tz))
        return int(croniter(expression, base, max_years_between_matches=8).get_next(datetime).timestamp())
    except (ValueError, KeyError, OverflowError) as exc:
        raise ValueError('无效 cron 或未来八年内无执行时间') from exc


def schedule_values(spec, now, tz):
    if spec.kind == 'cron':
        return None, next_cron(spec.cron, now, tz) if spec.enabled else _validate_disabled(spec.cron, now, tz)
    if spec.run_at is None or spec.run_at.tzinfo is None:
        raise ValueError('单次排程需要带时区的时间')
    if spec.run_at.second or spec.run_at.microsecond:
        raise ValueError('排程精确到分钟，秒必须为 0')
    ts = int(spec.run_at.timestamp())
    if spec.enabled and ts <= now:
        raise ValueError('单次排程必须晚于当前时间')
    return ts, ts if spec.enabled else None


def _validate_disabled(expression, now, tz):
    next_cron(expression, now, tz)
    return None


class Engine:
    def __init__(self, store, settings, display=None):
        self.store, self.settings = store, settings
        self.display = display or (MockDisplay(settings.mock_delay) if settings.driver == 'mock' else WaveshareDisplay())
        self.stop_event = threading.Event()
        self.threads = []

    def start(self):
        with self.store.connection() as db:
            interrupted = db.execute("SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()[0]
            db.execute("UPDATE jobs SET status='failed', error='进程中断，物理画面待重新刷新确认', finished_at=? WHERE status='running'", (now_ts(),))
            if interrupted:
                db.execute("UPDATE display_state SET status='unknown',error='上次刷新被中断，实际画面未知'")
        for target in (self.work_loop, self.schedule_loop):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self.threads.append(thread)

    def stop(self):
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=245)

    def enqueue(self, content_id, *, require_idle=True):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            content = db.execute('SELECT * FROM contents WHERE id=?', (content_id,)).fetchone()
            if not content:
                raise LookupError('内容不存在')
            # Check and insert under the same write transaction. This also covers
            # the gap before a newly accepted job is picked up by the worker.
            if require_idle and db.execute("SELECT id FROM jobs WHERE status IN ('pending','running') LIMIT 1").fetchone():
                raise DisplayBusyError('屏幕正在刷新或等待刷新，请完成后再提交')
            if db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('pending','running')").fetchone()[0] >= MAX_PENDING:
                raise ValueError('刷新队列已满，请稍后重试')
            job_id = new_id()
            db.execute("INSERT INTO jobs(id,content_id,title,status,created_at) VALUES(?,?,?,'pending',?)", (job_id, content_id, content['title'], now_ts()))
        return job_id

    def tick(self, now=None):
        now = now if now is not None else now_ts()
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            due = db.execute('SELECT s.*,c.title AS content_title FROM schedules s JOIN contents c ON c.id=s.content_id WHERE enabled=1 AND next_run<=? ORDER BY next_run,id', (now,)).fetchall()
            for item in due:
                late = now - item['next_run'] > GRACE_SECONDS
                full = db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('pending','running')").fetchone()[0] >= MAX_PENDING
                failed = late or full
                reason = '已错过执行时间（超过两分钟），不补播' if late else ('刷新队列已满，跳过本次' if full else None)
                db.execute('INSERT OR IGNORE INTO jobs(id,content_id,title,schedule_id,scheduled_for,status,error,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?)',
                           (new_id(), item['content_id'], item['content_title'], item['id'], item['next_run'], 'failed' if failed else 'pending', reason, now, now if failed else None))
                if item['kind'] == 'once':
                    db.execute('UPDATE schedules SET enabled=0,next_run=NULL WHERE id=?', (item['id'],))
                else:
                    following = next_cron(item['cron'], now, self.settings.timezone)
                    db.execute('UPDATE schedules SET next_run=? WHERE id=?', (following, item['id']))

    def process_one(self):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            job = db.execute("SELECT * FROM jobs WHERE status='pending' ORDER BY created_at, rowid LIMIT 1").fetchone()
            if not job:
                return False
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (job['id'],))
            db.execute("UPDATE display_state SET status='refreshing',error=NULL")
        try:
            if not job['content_id']:
                raise ValueError('内容已删除')
            with Image.open(self.store.image_path(job['content_id'])) as opened:
                image = opened.convert('RGB')
            self.display.display(image)
            snapshot = f"screen-{job['id']}.png"
            image.save(self.store.root / snapshot)
            with self.store.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                previous = db.execute('SELECT snapshot FROM display_state').fetchone()[0]
                # Content can be renamed while the physical refresh is in progress.
                title = db.execute('SELECT title FROM contents WHERE id=?', (job['content_id'],)).fetchone()[0]
                db.execute("UPDATE jobs SET title=?,status='done',finished_at=? WHERE id=?", (title, now_ts(), job['id']))
                db.execute('UPDATE contents SET last_displayed=? WHERE id=?', (now_ts(), job['content_id']))
                db.execute("UPDATE display_state SET content_id=?,title=?,updated_at=?,snapshot=?,status='idle',error=NULL", (job['content_id'], title, now_ts(), snapshot))
            if previous:
                try:
                    (self.store.root / previous).unlink(missing_ok=True)
                except OSError:
                    log.warning('Old snapshot cleanup failed', exc_info=True)
        except Exception as exc:
            log.exception('Display job failed: %s', job['id'])
            with self.store.connection() as db:
                db.execute("UPDATE jobs SET status='failed',error=?,finished_at=? WHERE id=?", (str(exc), now_ts(), job['id']))
                db.execute("UPDATE display_state SET status='error',error=?", (f'刷新失败，实际画面可能不完整：{exc}',))
        return True

    def schedule_loop(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                log.exception('Scheduler tick failed')
            self.stop_event.wait(1)

    def work_loop(self):
        while not self.stop_event.is_set():
            try:
                worked = self.process_one()
            except Exception:
                log.exception('Display worker failed')
                worked = False
            if not worked:
                self.stop_event.wait(.25)

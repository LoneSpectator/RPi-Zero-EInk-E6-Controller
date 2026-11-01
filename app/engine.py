"""Durable serial display queue and minute-resolution scheduler (single process)."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import logging
import json
import threading
import time
from croniter import croniter
from PIL import Image
from .store import now_ts, new_id
from .hardware import MockDisplay, WaveshareDisplay, battery_status
from .battery import BatteryMonitor, LOW_BATTERY_PERCENT
from .render import battery_warning
from .wallpaper import fetch_bing_wallpaper, has_displayable_content, WallpaperCancelled

log = logging.getLogger(__name__)
GRACE_SECONDS = 120
MAX_PENDING = 20
BING_RETRY_SECONDS = 30


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
    def __init__(self, store, settings, display=None, wallpaper_fetcher=None):
        self.store, self.settings = store, settings
        self.display = display or (MockDisplay(settings.mock_delay) if settings.driver == 'mock' else WaveshareDisplay())
        self.battery = BatteryMonitor(store, settings.driver, battery_status)
        self.battery_clear_since = None
        self.stop_event = threading.Event()
        self.threads = []
        self.next_log_cleanup = 0
        self.wallpaper_fetcher = wallpaper_fetcher or fetch_bing_wallpaper
        self.startup_wallpaper = {'status': 'pending' if settings.bing_default_wallpaper else 'disabled', 'message': ''}

    def start(self):
        with self.store.connection() as db:
            interrupted = db.execute("SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()[0]
            db.execute("UPDATE jobs SET status='failed', error='进程中断，物理画面待重新刷新确认', finished_at=? WHERE status='running'", (now_ts(),))
            if interrupted:
                db.execute("UPDATE display_state SET status='unknown',error='上次刷新被中断，实际画面未知'")
        self.store.prune_jobs()
        self.next_log_cleanup = time.monotonic() + 60
        for target in (self.work_loop, self.schedule_loop):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self.threads.append(thread)
        if self.settings.bing_default_wallpaper:
            thread = threading.Thread(target=self.bootstrap_wallpaper, daemon=True)
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

    def bootstrap_wallpaper(self):
        while not self.stop_event.is_set():
            try:
                if has_displayable_content(self.store):
                    self.startup_wallpaper = {'status': 'skipped', 'message': ''}
                    return
                self.startup_wallpaper = {'status': 'downloading', 'message': '正在获取 Bing 今日壁纸，获取后将自动刷新到屏幕。'}
                wallpaper = self.wallpaper_fetcher(self.stop_event)
                if self.stop_event.is_set():
                    return
                installed = self.install_startup_wallpaper(wallpaper)
                self.startup_wallpaper = {'status': 'queued' if installed else 'skipped', 'message': ''}
                return
            except WallpaperCancelled:
                return
            except Exception:
                log.warning('Bing startup wallpaper unavailable; will retry', exc_info=True)
                self.startup_wallpaper = {'status': 'retrying', 'message': '暂时无法获取 Bing 今日壁纸，稍后自动重试；也可上传自己的内容。'}
                deadline = time.monotonic() + BING_RETRY_SECONDS
                while time.monotonic() < deadline:
                    if self.stop_event.wait(min(1, max(0, deadline - time.monotonic()))):
                        return
                    if has_displayable_content(self.store):
                        self.startup_wallpaper = {'status': 'skipped', 'message': ''}
                        return

    def install_startup_wallpaper(self, wallpaper):
        if not 1 <= len(wallpaper.title.strip()) <= 120 or wallpaper.image.size != (800, 480):
            raise ValueError('默认壁纸内容无效')
        content_id, job_id = new_id(), new_id()
        path = self.store.image_path(content_id)
        installed = False
        try:
            wallpaper.image.save(path, format='PNG')
            with self.store.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                # A user may upload/display their own content while the network
                # download is in flight. Recheck before committing anything.
                if self.stop_event.is_set() or has_displayable_content(self.store, db):
                    return None
                if db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('pending','running')").fetchone()[0] >= MAX_PENDING:
                    raise ValueError('刷新队列已满，稍后重试默认壁纸')
                db.execute('INSERT INTO contents(id,title,kind,created_at,payload) VALUES(?,?,?,?,?)',
                           (content_id, wallpaper.title.strip(), 'image', now_ts(), json.dumps(wallpaper.payload, ensure_ascii=False)))
                db.execute("INSERT INTO jobs(id,content_id,title,status,created_at,purpose) VALUES(?,?,?,'pending',?,'startup_wallpaper')",
                           (job_id, content_id, wallpaper.title.strip(), now_ts()))
            installed = True
            return {'content_id': content_id, 'job_id': job_id}
        finally:
            if not installed:
                path.unlink(missing_ok=True)

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
            if not job['content_id'] and not job['source_snapshot']:
                raise ValueError('内容已删除')
            path = self.store.root / job['source_snapshot'] if job['source_snapshot'] else self.store.image_path(job['content_id'])
            with Image.open(path) as opened:
                source = opened.convert('RGB')
            low = self.low_battery()
            image = battery_warning(source, low)
            self.display.display(image)
            snapshot = f"screen-{job['id']}.png"
            source_snapshot = f"screen-source-{job['id']}.png"
            image.save(self.store.root / snapshot)
            source.save(self.store.root / source_snapshot)
            with self.store.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                previous = db.execute('SELECT snapshot,source_snapshot FROM display_state').fetchone()
                # Content can be renamed while the physical refresh is in progress.
                content = db.execute('SELECT title FROM contents WHERE id=?', (job['content_id'],)).fetchone()
                title = content[0] if content else job['title']
                db.execute("UPDATE jobs SET title=?,status='done',finished_at=? WHERE id=?", (title, now_ts(), job['id']))
                db.execute('UPDATE contents SET last_displayed=? WHERE id=?', (now_ts(), job['content_id']))
                db.execute("UPDATE display_state SET content_id=?,title=?,updated_at=?,snapshot=?,source_snapshot=?,battery_warning=?,status='idle',error=NULL",
                           (job['content_id'], title, now_ts(), snapshot, source_snapshot, int(low)))
            for previous_path in set(previous) - {None, snapshot, source_snapshot}:
                try:
                    (self.store.root / previous_path).unlink(missing_ok=True)
                except OSError:
                    log.warning('Old snapshot cleanup failed', exc_info=True)
        except Exception as exc:
            log.exception('Display job failed: %s', job['id'])
            with self.store.connection() as db:
                db.execute("UPDATE jobs SET status='failed',error=?,finished_at=? WHERE id=?", (str(exc), now_ts(), job['id']))
                db.execute("UPDATE display_state SET status='error',error=?", (f'刷新失败，实际画面可能不完整：{exc}',))
        return True

    def low_battery(self):
        battery = self.battery.status()
        if battery['available'] and battery['percent'] is not None:
            return battery['percent'] < LOW_BATTERY_PERCENT
        # An unavailable sample must not erase an existing physical warning.
        return bool(self.store.one('SELECT battery_warning FROM display_state')['battery_warning'])

    def update_battery_warning(self):
        battery = self.battery.status()
        if not battery['available'] or battery['percent'] is None:
            self.battery_clear_since = None
            return
        low = battery['percent'] < LOW_BATTERY_PERCENT
        # Require sustained recovery before spending another e-paper refresh to
        # remove the border. This avoids repeated refreshes around 19/20%.
        if low:
            self.battery_clear_since = None
        elif self.battery_clear_since is None:
            self.battery_clear_since = now_ts()
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            state = db.execute('SELECT * FROM display_state').fetchone()
            if not state['snapshot'] or state['status'] != 'idle' or bool(state['battery_warning']) == low:
                return
            if not low and now_ts() - self.battery_clear_since < 30:
                return
            if db.execute("SELECT 1 FROM jobs WHERE status IN ('pending','running') LIMIT 1").fetchone():
                return
            # Serialize automatic overlays with normal jobs, using the pristine
            # successful screen source even if its library entry was deleted.
            content = db.execute('SELECT id FROM contents WHERE id=?', (state['content_id'],)).fetchone()
            db.execute('INSERT INTO jobs(id,content_id,title,status,created_at,source_snapshot,purpose) VALUES(?,?,?,?,?,?,?)',
                       (new_id(), content[0] if content else None, state['title'] or '当前画面', 'pending', now_ts(),
                        state['source_snapshot'] or state['snapshot'], 'battery_warning'))

    def schedule_loop(self):
        while not self.stop_event.is_set():
            try:
                if time.monotonic() >= self.next_log_cleanup:
                    self.store.prune_jobs()
                    self.next_log_cleanup = time.monotonic() + 60
                self.tick()
                self.update_battery_warning()
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

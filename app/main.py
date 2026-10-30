from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
import fcntl
import json
import secrets
import sqlite3
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Request
from fastapi.responses import FileResponse, Response, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from .limits import RequestSizeLimit
from .config import Settings
from .models import TextContent, ScheduleInput
from .render import render_text, render_image, png, font_status
from .store import Store, new_id, now_ts
from .engine import Engine, schedule_values
from .hardware import battery_status

STATIC = Path(__file__).parent / 'static'
MAX_UPLOAD = 12 * 1024 * 1024
basic = HTTPBasic(auto_error=False)


def create_app(settings=None, display=None):
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        lock = open(settings.data_dir / '.instance.lock', 'a')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise RuntimeError('同一数据目录仅允许运行一个进程；请使用 --workers 1')
        try:
            app.state.store = Store(settings.data_dir)
            app.state.engine = Engine(app.state.store, settings, display)
            app.state.engine.start()
            yield
        finally:
            if hasattr(app.state, 'engine'):
                app.state.engine.stop()
            lock.close()

    app = FastAPI(title='墨间 · PhotoPainter', lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(RequestSizeLimit)
    app.state.settings = settings

    def auth(credentials: HTTPBasicCredentials | None = Depends(basic)):
        if not settings.password:
            return
        if credentials and secrets.compare_digest(credentials.username.encode(), settings.username.encode()) and secrets.compare_digest(credentials.password.encode(), settings.password.encode()):
            return
        raise HTTPException(401, '需要登录', headers={'WWW-Authenticate': 'Basic realm="PhotoPainter", charset="UTF-8"'})

    @app.middleware('http')
    async def guard(request: Request, call_next):
        # Refuse cross-origin mutations, including browser form CSRF with cached Basic auth.
        origin = request.headers.get('origin')
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and origin:
            from urllib.parse import urlsplit
            parsed = urlsplit(origin)
            if parsed.netloc != request.headers.get('host') or parsed.scheme not in ('http', 'https'):
                return JSONResponse({'detail': '拒绝跨站请求'}, status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=422)

    @app.exception_handler(LookupError)
    async def missing(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=404)

    def store():
        return app.state.store

    def content(content_id):
        item = store().one('SELECT * FROM contents WHERE id=?', (content_id,))
        if not item:
            raise HTTPException(404, '内容不存在')
        return item

    def save_content(title, kind, payload, image):
        title = title.strip()
        if not 1 <= len(title) <= 120:
            raise ValueError('名称需要 1–120 个字符')
        content_id = new_id()
        path = store().image_path(content_id)
        image.save(path)
        try:
            with store().connection() as db:
                db.execute('INSERT INTO contents(id,title,kind,created_at,payload) VALUES(?,?,?,?,?)', (content_id, title, kind, now_ts(), json.dumps(payload, ensure_ascii=False)))
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return content(content_id)

    def upload_data(upload):
        data = upload.file.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, '图片不能超过 12 MiB')
        return data

    @app.get('/', dependencies=[Depends(auth)])
    def index():
        return FileResponse(STATIC / 'index.html')

    @app.get('/api/health', dependencies=[Depends(auth)])
    def health():
        store().one('SELECT 1')
        return {'status': 'ok', 'driver': settings.driver}

    @app.get('/api/status', dependencies=[Depends(auth)])
    def status():
        state = store().one('SELECT * FROM display_state')
        running = store().one("SELECT id,title FROM jobs WHERE status='running' LIMIT 1")
        return {'now': datetime.now(ZoneInfo(settings.timezone)).isoformat(), 'timezone': settings.timezone,
                'driver': settings.driver, 'battery': battery_status(settings.driver), 'screen': state,
                'running': running, 'pending': store().one("SELECT COUNT(*) AS n FROM jobs WHERE status='pending'")['n'],
                'fonts': font_status(), 'auth_enabled': bool(settings.password),
                'upcoming': store().rows('SELECT s.*,c.title AS content_title FROM schedules s JOIN contents c ON c.id=s.content_id WHERE enabled=1 ORDER BY next_run LIMIT 6'),
                'jobs': store().rows('SELECT * FROM jobs ORDER BY created_at DESC,rowid DESC LIMIT 12')}

    @app.get('/api/screen', dependencies=[Depends(auth)])
    def screen():
        state = store().one('SELECT * FROM display_state')
        if not state['snapshot']:
            raise HTTPException(404, '还没有成功刷新的画面')
        return FileResponse(store().root / state['snapshot'], media_type='image/png')

    @app.post('/api/preview/text', dependencies=[Depends(auth)])
    def preview_text(doc: TextContent):
        return Response(png(render_text(doc)), media_type='image/png')

    @app.post('/api/contents/text', dependencies=[Depends(auth)], status_code=201)
    def create_text(doc: TextContent):
        return save_content(doc.title, 'text', doc.model_dump(), render_text(doc))

    @app.post('/api/preview/image', dependencies=[Depends(auth)])
    def preview_image(file: UploadFile = File(), mode: str = Form('contain')):
        return Response(png(render_image(upload_data(file), mode)), media_type='image/png')

    @app.post('/api/contents/image', dependencies=[Depends(auth)], status_code=201)
    def create_image(file: UploadFile = File(), title: str = Form('图片内容'), mode: str = Form('contain')):
        return save_content(title, 'image', {'mode': mode}, render_image(upload_data(file), mode))

    @app.get('/api/contents', dependencies=[Depends(auth)])
    def list_contents():
        return store().rows('SELECT id,title,kind,created_at,last_displayed FROM contents ORDER BY created_at DESC,rowid DESC')

    @app.get('/api/contents/{content_id}/image', dependencies=[Depends(auth)])
    def content_image(content_id: str):
        content(content_id)
        return FileResponse(store().image_path(content_id), media_type='image/png')

    @app.delete('/api/contents/{content_id}', dependencies=[Depends(auth)])
    def delete_content(content_id: str):
        with store().connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT id FROM contents WHERE id=?', (content_id,)).fetchone():
                raise HTTPException(404, '内容不存在')
            if db.execute("SELECT id FROM jobs WHERE content_id=? AND status IN ('pending','running')", (content_id,)).fetchone():
                raise HTTPException(409, '内容正在刷新或等待刷新')
            try:
                db.execute('DELETE FROM contents WHERE id=?', (content_id,))
            except sqlite3.IntegrityError:
                raise HTTPException(409, '请先删除引用该内容的排程（含已停用排程）')
        store().image_path(content_id).unlink(missing_ok=True)
        return {'deleted': True}

    @app.post('/api/contents/{content_id}/display', dependencies=[Depends(auth)], status_code=202)
    def show(content_id: str):
        return {'job_id': app.state.engine.enqueue(content_id)}

    @app.get('/api/jobs/{job_id}', dependencies=[Depends(auth)])
    def job(job_id: str):
        item = store().one('SELECT * FROM jobs WHERE id=?', (job_id,))
        if not item:
            raise HTTPException(404, '刷新任务不存在')
        return item

    @app.get('/api/schedules', dependencies=[Depends(auth)])
    def list_schedules():
        return store().rows('SELECT s.*,c.title AS content_title FROM schedules s JOIN contents c ON c.id=s.content_id ORDER BY enabled DESC,next_run,created_at DESC')

    def save_schedule(spec, schedule_id=None):
        run_at, following = schedule_values(spec, now_ts(), settings.timezone)
        with store().connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT id FROM contents WHERE id=?', (spec.content_id,)).fetchone():
                raise HTTPException(404, '内容不存在')
            values = (spec.title.strip(), spec.content_id, spec.kind, run_at, spec.cron if spec.kind == 'cron' else None, int(spec.enabled), following)
            if not values[0]:
                raise ValueError('排程名称不能为空')
            if schedule_id:
                result = db.execute('UPDATE schedules SET title=?,content_id=?,kind=?,run_at=?,cron=?,enabled=?,next_run=? WHERE id=?', values + (schedule_id,))
                if not result.rowcount:
                    raise HTTPException(404, '排程不存在')
            else:
                schedule_id = new_id()
                db.execute('INSERT INTO schedules(title,content_id,kind,run_at,cron,enabled,next_run,id,created_at) VALUES(?,?,?,?,?,?,?,?,?)', values + (schedule_id, now_ts()))
        return store().one('SELECT * FROM schedules WHERE id=?', (schedule_id,))

    @app.post('/api/schedules', dependencies=[Depends(auth)], status_code=201)
    def create_schedule(spec: ScheduleInput):
        return save_schedule(spec)

    @app.put('/api/schedules/{schedule_id}', dependencies=[Depends(auth)])
    def update_schedule(schedule_id: str, spec: ScheduleInput):
        return save_schedule(spec, schedule_id)

    @app.delete('/api/schedules/{schedule_id}', dependencies=[Depends(auth)])
    def delete_schedule(schedule_id: str):
        with store().connection() as db:
            if not db.execute('DELETE FROM schedules WHERE id=?', (schedule_id,)).rowcount:
                raise HTTPException(404, '排程不存在')
        return {'deleted': True}

    app.mount('/static', StaticFiles(directory=STATIC), name='static')
    return app


app = create_app()

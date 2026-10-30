from datetime import datetime, timezone
from io import BytesIO
import time
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import Settings
from app.main import create_app
from app.models import TextContent, ScheduleInput
from app.render import render_image, render_text, COLORS, font_status
from app.store import Store, now_ts
from app.engine import Engine, next_cron, schedule_values, MAX_PENDING


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, driver='mock', timezone='Asia/Shanghai', password='', mock_delay=.01)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as client:
        yield client


def image_bytes(color='red', size=(120, 240), fmt='PNG'):
    out = BytesIO()
    Image.new('RGB', size, color).save(out, format=fmt)
    return out.getvalue()


def upload(client, title='照片'):
    response = client.post('/api/contents/image', files={'file': ('photo.png', image_bytes(), 'image/png')}, data={'title': title, 'mode': 'contain'})
    assert response.status_code == 201, response.text
    return response.json()


def wait_job(client, job_id):
    for _ in range(100):
        result = client.get('/api/jobs/' + job_id).json()
        if result['status'] in ('done', 'failed'):
            return result
        time.sleep(.02)
    pytest.fail('Display worker did not finish')


def test_health_and_empty_state(client):
    assert client.get('/').status_code == 200
    assert client.get('/api/health').json() == {'status': 'ok', 'driver': 'mock'}
    status = client.get('/api/status').json()
    assert status['screen']['status'] == 'unknown'
    assert status['battery']['percent'] is None
    assert client.get('/api/screen').status_code == 404


@pytest.mark.parametrize('fmt', ['JPEG', 'PNG'])
def test_fit_cover_and_palette(fmt):
    data = image_bytes(fmt=fmt)
    fitted, filled = render_image(data, 'contain'), render_image(data, 'cover')
    assert fitted.size == filled.size == (800, 480)
    assert fitted.getpixel((0, 240)) == (255, 255, 255)
    assert fitted.getpixel((400, 240)) == (255, 0, 0)
    assert filled.getpixel((0, 240)) == (255, 0, 0)
    assert set(filled.get_flattened_data()) <= set(COLORS)


def test_transparency_composited_white():
    im = Image.new('RGBA', (100, 100), (255, 0, 0, 0))
    buf = BytesIO(); im.save(buf, 'PNG')
    assert render_image(buf.getvalue(), 'cover').getpixel((200, 200)) == (255, 255, 255)


def test_upload_invalid_files_and_mode(client):
    for data, mode in [(b'bad', 'contain'), (image_bytes(), 'stretch'), (image_bytes(fmt='GIF'), 'cover')]:
        response = client.post('/api/contents/image', files={'file': ('x.png', data)}, data={'mode': mode})
        assert response.status_code == 422
    assert client.get('/api/contents').json() == []


def test_preview_then_show_and_delete_preserves_current_screen(client):
    data = image_bytes()
    preview = client.post('/api/preview/image', files={'file': ('x.png', data)}, data={'mode': 'contain'})
    assert preview.status_code == 200
    assert client.get('/api/contents').json() == []  # Previews do not save content.
    content = upload(client)
    assert content['last_displayed'] is None
    accepted = client.post(f"/api/contents/{content['id']}/display")
    assert accepted.status_code == 202
    result = wait_job(client, accepted.json()['job_id'])
    assert result['status'] == 'done'
    screen = client.get('/api/screen').content
    assert screen == preview.content
    assert client.get('/api/contents').json()[0]['last_displayed']
    assert client.delete(f"/api/contents/{content['id']}").status_code == 200
    assert client.get('/api/screen').content == screen
    assert client.get('/api/contents').json() == []


def test_text_preview_styles_palette_and_overflow(client):
    doc = {'title': '诗', 'paragraphs': [{'align': 'center', 'runs': [
        {'text': '日子缓缓，', 'size': 40, 'bold': True, 'color': '#ff0000'},
        {'text': '万物可爱。', 'size': 32, 'italic': True, 'font': 'source-serif'}]}]}
    preview = client.post('/api/preview/text', json=doc)
    assert preview.status_code == 200, preview.text
    im = Image.open(BytesIO(preview.content))
    assert im.size == (800, 480)
    assert set(im.get_flattened_data()) <= set(COLORS)
    content = client.post('/api/contents/text', json=doc)
    assert content.status_code == 201
    assert client.get(f"/api/contents/{content.json()['id']}/image").content == preview.content
    doc['paragraphs'][0]['runs'][0]['text'] = '超出屏幕' * 100
    assert client.post('/api/preview/text', json=doc).status_code == 422


@pytest.mark.parametrize('field,value', [('color', '#884422'), ('font', 'evil'), ('size', 500)])
def test_text_rejects_unsupported_styles(client, field, value):
    doc = {'paragraphs': [{'runs': [{'text': 'text', field: value}]}]}
    assert client.post('/api/preview/text', json=doc).status_code == 422


@pytest.mark.parametrize('name', ['heiti', 'songti', 'kaiti', 'source-sans', 'source-serif'])
def test_all_installed_fonts_render(name):
    if not font_status()[name]['available']:
        pytest.skip(f'{name} not installed on this host; validate in Docker')
    image = render_text(TextContent(paragraphs=[{'runs': [{'text': '中文字体 Abc 123', 'font': name}]}]))
    assert len(image.getcolors()) > 1


def test_schedule_crud_and_reference_protection(client):
    item = upload(client)
    spec = {'title': '每半小时', 'content_id': item['id'], 'kind': 'cron', 'cron': '*/30 * * * *'}
    response = client.post('/api/schedules', json=spec)
    assert response.status_code == 201, response.text
    schedule = response.json()
    assert datetime.fromtimestamp(schedule['next_run'], timezone.utc).minute in (0, 30)
    assert len(client.get('/api/schedules').json()) == 1
    assert client.delete('/api/contents/' + item['id']).status_code == 409
    spec.update(enabled=False, title='暂停')
    updated = client.put('/api/schedules/' + schedule['id'], json=spec)
    assert updated.status_code == 200 and updated.json()['next_run'] is None
    assert client.delete('/api/contents/' + item['id']).status_code == 409
    assert client.delete('/api/schedules/' + schedule['id']).status_code == 200
    assert client.delete('/api/contents/' + item['id']).status_code == 200
    assert client.get('/api/schedules').json() == []


@pytest.mark.parametrize('expression', ['* * * * * *', '@hourly', 'bad', '0 25 * * *', '0 0 31 2 *'])
def test_invalid_cron(client, expression):
    item = upload(client)
    response = client.post('/api/schedules', json={'title': 'bad', 'content_id': item['id'], 'kind': 'cron', 'cron': expression})
    assert response.status_code == 422


def test_once_requires_future_minute_and_timezone():
    now = int(datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp())
    for date in ['2030-01-02T08:00:01+08:00', '2030-01-02T08:00:00', '2029-01-02T08:00:00+08:00']:
        with pytest.raises(ValueError):
            schedule_values(ScheduleInput(title='one',content_id='x',kind='once',run_at=date),now,'Asia/Shanghai')
    run, next_run = schedule_values(ScheduleInput(title='one',content_id='x',kind='once',run_at='2030-01-02T08:00:00+08:00'),now,'Asia/Shanghai')
    assert run == next_run == now + 86400


def deterministic_engine(settings):
    store = Store(settings.data_dir)
    with store.connection() as db:
        db.execute("INSERT INTO contents VALUES('c','title','image',?,NULL,'{}')", (now_ts(),))
    Image.new('RGB', (800, 480), 'red').save(store.image_path('c'))
    return Engine(store, settings), store


def test_scheduler_due_once_cron_and_dedup(settings):
    engine, store = deterministic_engine(settings)
    now = 1900000020
    with store.connection() as db:
        for sid, kind in [('one','once'), ('repeat','cron')]:
            db.execute('INSERT INTO schedules VALUES(?,?,?,?,?,?,?,?,?)', (sid,sid,'c',kind,now,'*/30 * * * *',1,now,now))
    engine.tick(now); engine.tick(now)
    assert len(store.rows('SELECT * FROM jobs')) == 2
    once = store.one("SELECT * FROM schedules WHERE id='one'")
    assert once['enabled'] == 0 and once['next_run'] is None
    assert store.one("SELECT * FROM schedules WHERE id='repeat'")['next_run'] > now
    assert engine.process_one() is True
    assert store.one("SELECT * FROM display_state")['status'] == 'idle'


def test_missed_schedule_not_replayed(settings):
    engine, store = deterministic_engine(settings)
    with store.connection() as db:
        db.execute("INSERT INTO schedules VALUES('one','one','c','once',0,NULL,1,1,0)")
    engine.tick(300)
    job = store.one('SELECT * FROM jobs')
    assert job['status'] == 'failed' and '不补播' in job['error']
    assert engine.process_one() is False


def test_worker_failure_never_updates_current(settings):
    engine, store = deterministic_engine(settings)
    class BrokenDisplay:
        def display(self, image):
            raise OSError('SPI disconnected')
    engine.display = BrokenDisplay()
    engine.enqueue('c'); engine.process_one()
    state = store.one('SELECT * FROM display_state')
    assert state['status'] == 'error' and state['updated_at'] is None and state['snapshot'] is None
    assert store.one('SELECT * FROM jobs')['status'] == 'failed'


def test_recovery_and_persistent_pending(settings):
    engine, store = deterministic_engine(settings)
    first = engine.enqueue('c'); second = engine.enqueue('c')
    with store.connection() as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (first,))
    recovered = Engine(Store(settings.data_dir), settings)
    recovered.start()
    try:
        deadline = time.monotonic() + 3
        while store.one('SELECT * FROM jobs WHERE id=?', (second,))['status'] != 'done' and time.monotonic() < deadline:
            time.sleep(.02)
        assert store.one('SELECT * FROM jobs WHERE id=?', (first,))['status'] == 'failed'
        assert store.one('SELECT * FROM jobs WHERE id=?', (second,))['status'] == 'done'
    finally:
        recovered.stop()


def test_queue_capacity(settings):
    engine, _ = deterministic_engine(settings)
    for _ in range(MAX_PENDING):engine.enqueue('c')
    with pytest.raises(ValueError,match='队列已满'):engine.enqueue('c')


def test_auth_and_cross_origin(settings):
    settings.password = 'test-password'
    with TestClient(create_app(settings)) as client:
        assert client.get('/api/status').status_code == 401
        assert client.get('/').status_code == 401
        assert client.get('/api/status', auth=('admin','bad')).status_code == 401
        assert client.get('/api/status', auth=('admin','test-password')).status_code == 200
        assert client.post('/api/schedules',headers={'Origin':'https://evil.example'},json={},auth=('admin','test-password')).status_code == 403


def test_missing_ids_and_path_traversal(client):
    assert client.post('/api/contents/missing/display').status_code == 404
    assert client.get('/api/contents/missing/image').status_code == 404
    assert client.delete('/api/schedules/missing').status_code == 404
    assert client.get('/api/contents/%2e%2e%2fLICENSE/image').status_code == 404


def test_restart_preserves_content_and_schedule(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        item = upload(client)
        spec = {'title':'每天','content_id':item['id'],'kind':'cron','cron':'0 8 * * *'}
        assert client.post('/api/schedules',json=spec).status_code == 201
    with TestClient(create_app(settings)) as client:
        assert client.get('/api/contents').json()[0]['id'] == item['id']
        assert client.get('/api/schedules').json()[0]['cron'] == '0 8 * * *'


def test_single_instance_lock(settings):
    with TestClient(create_app(settings)):
        with pytest.raises(RuntimeError, match='一个进程'):
            with TestClient(create_app(settings)):pass


def test_vendor_buffer_contains_only_six_codes():
    from app.vendor.epd7in3e import EPD
    packed = EPD().getbuffer(Image.new('RGB',(800,480),'red'))
    assert len(packed) == 192000
    assert set(packed) == {0x33}


def test_oversized_request_is_rejected_before_parsing(client):
    response = client.post('/api/preview/text', content=b'{}', headers={'Content-Length': str(14*1024*1024), 'Content-Type': 'application/json'})
    assert response.status_code == 413


def test_busy_timeout_and_gpio_cleanup(monkeypatch):
    from app.hardware import WaveshareDisplay
    from app.vendor import epd7in3e, epdconfig
    cleaned = []
    monkeypatch.setenv('EPD_BUSY_TIMEOUT', '0')
    monkeypatch.setattr(epdconfig, 'module_init', lambda: 0)
    monkeypatch.setattr(epdconfig, 'module_exit', lambda: cleaned.append(True))
    monkeypatch.setattr(epdconfig, 'digital_write', lambda *args: None)
    monkeypatch.setattr(epdconfig, 'digital_read', lambda *args: 0)
    monkeypatch.setattr(epdconfig, 'delay_ms', lambda *args: None)
    with pytest.raises(TimeoutError, match='BUSY'):
        WaveshareDisplay().display(Image.new('RGB',(800,480)))
    assert cleaned


def test_battery_voltage_endianness_and_unavailable(monkeypatch):
    import sys
    from types import SimpleNamespace
    from app.hardware import battery_status
    class Bus:
        def __init__(self, index):assert index == 1
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read_i2c_block_data(self, addr, reg, length):
            assert addr == 0x43
            return [0x39, 0x9f] if reg == 0 else [0x1c, 0x20]  # 3.6 V
        def write_i2c_block_data(self,*args):pass
    monkeypatch.setitem(sys.modules,'smbus2',SimpleNamespace(SMBus=Bus))
    result = battery_status('waveshare')
    assert result['percent'] == 50 and result['voltage'] == 3.6 and result['estimated']
    class MissingBus:
        def __init__(self,*args):raise OSError('no I2C')
    monkeypatch.setitem(sys.modules,'smbus2',SimpleNamespace(SMBus=MissingBus))
    assert battery_status('waveshare')['percent'] is None


def test_preview_image_preserves_exif_orientation():
    im = Image.new('RGB', (100,200), 'red')
    exif=im.getexif(); exif[274]=6
    buf=BytesIO();im.save(buf,'JPEG',exif=exif)
    rendered=render_image(buf.getvalue(),'contain')
    assert rendered.getpixel((400,10)) == (255,255,255)
    assert rendered.getpixel((20,240)) == (255,0,0)

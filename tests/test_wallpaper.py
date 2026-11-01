from io import BytesIO
import json
from threading import Event
import time

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from app.config import Settings
from app.engine import Engine, MAX_PENDING
from app.main import create_app
from app.render import COLORS
from app.store import Store
from app.wallpaper import (
    METADATA_PATH, Wallpaper, WallpaperCancelled, bing_url, download,
    fetch_bing_wallpaper, has_displayable_content,
)


def image_data():
    data = BytesIO()
    Image.new('RGB', (1200, 800), 'blue').save(data, 'JPEG')
    return data.getvalue()


def default_wallpaper():
    return Wallpaper('Bing 今日壁纸 · 2026-11-02', Image.new('RGB', (800, 480), 'blue'),
                     {'source': 'bing', 'mode': 'cover', 'date': '2026-11-02', 'url': 'https://www.bing.com/th?id=test'})


def wait_for(predicate):
    deadline = time.monotonic() + 4
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail('Startup wallpaper did not reach expected state')
        time.sleep(.02)


class Response(BytesIO):
    def __init__(self, data, headers=None):
        super().__init__(data)
        self.headers = headers or {'Content-Length': str(len(data))}


class Opener:
    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def open(self, request, timeout):
        assert 0 < timeout <= 10
        self.calls.append(request.full_url)
        reply = self.replies[request.full_url]
        if isinstance(reply, Exception):
            raise reply
        return Response(reply)


def test_bing_download_uses_today_metadata_and_real_image_conversion(monkeypatch):
    metadata = {'images': [{'startdate': '20261102', 'url': '/th?id=test.jpg', 'title': '风景', 'copyright': '作品署名'}]}
    opener = Opener({'https://www.bing.com' + METADATA_PATH: json.dumps(metadata).encode(),
                     'https://www.bing.com/th?id=test.jpg': image_data()})
    monkeypatch.setattr('app.wallpaper.build_opener', lambda *args: opener)
    result = fetch_bing_wallpaper(Event())
    assert result.title == 'Bing 今日壁纸 · 2026-11-02'
    assert result.image.size == (800, 480)
    assert set(result.image.get_flattened_data()) <= set(COLORS)
    assert result.payload['source'] == 'bing' and result.payload['mode'] == 'cover'
    assert result.payload['copyright'] == '作品署名'
    assert opener.calls == ['https://www.bing.com' + METADATA_PATH, 'https://www.bing.com/th?id=test.jpg']


def test_metadata_host_fallback(monkeypatch):
    metadata = {'images': [{'startdate': '20261102', 'url': '/th?id=test.jpg'}]}
    opener = Opener({'https://www.bing.com' + METADATA_PATH: OSError('unavailable'),
                     'https://cn.bing.com' + METADATA_PATH: json.dumps(metadata).encode(),
                     'https://cn.bing.com/th?id=test.jpg': image_data()})
    monkeypatch.setattr('app.wallpaper.build_opener', lambda *args: opener)
    assert fetch_bing_wallpaper(Event()).payload['url'] == 'https://cn.bing.com/th?id=test.jpg'


@pytest.mark.parametrize('url', ['http://www.bing.com/image', 'https://other.example/image',
                               'https://www.bing.com:8443/image', 'https://user@www.bing.com/image',
                               'https://127.0.0.1/image'])
def test_image_urls_stay_on_official_https_hosts(url):
    with pytest.raises(ValueError):
        bing_url(url)


def test_download_size_timeout_and_cancellation_are_bounded():
    opener = Opener({'https://www.bing.com/image': b'x' * 100})
    with pytest.raises(ValueError, match='过大'):
        download(opener, 'https://www.bing.com/image', 20, time.monotonic() + 5, Event())
    with pytest.raises(TimeoutError):
        download(opener, 'https://www.bing.com/image', 200, time.monotonic() - 1, Event())
    stopped = Event(); stopped.set()
    with pytest.raises(WallpaperCancelled):
        fetch_bing_wallpaper(stopped)


class Display:
    def __init__(self): self.images = []
    def display(self, image): self.images.append(image.copy())


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / 'new-data', driver='mock', password='',
                    mock_delay=.01, bing_default_wallpaper=True)


def test_missing_data_downloads_saves_and_refreshes_then_skips_on_restart(settings):
    calls = []
    def fetcher(stop):
        calls.append(True)
        return default_wallpaper()
    display = Display()
    assert not settings.data_dir.exists()
    app = create_app(settings, display, fetcher)
    with TestClient(app) as client:
        wait_for(lambda: app.state.store.one("SELECT id FROM jobs WHERE status='done'") is not None)
        assert len(calls) == 1 and len(display.images) == 1
        item = client.get('/api/contents').json()[0]
        assert item['title'] == default_wallpaper().title and item['kind'] == 'image'
        assert app.state.store.image_path(item['id']).is_file()
        job = app.state.store.one('SELECT * FROM jobs')
        assert job['content_id'] == item['id'] and job['purpose'] == 'startup_wallpaper'
        assert display.images[0].getpixel((400, 240)) == (0, 0, 255)
        assert client.get('/api/screen').status_code == 200
    second = Display()
    app = create_app(settings, second, fetcher)
    with TestClient(app):
        wait_for(lambda: app.state.engine.startup_wallpaper['status'] == 'skipped')
        assert len(calls) == 1 and second.images == []
        assert len(app.state.store.rows('SELECT * FROM jobs')) == 1


@pytest.mark.parametrize('existing', ['content', 'snapshot', 'source', 'queued-source'])
def test_existing_displayable_content_or_saved_screen_skips_download(settings, existing):
    settings.data_dir.mkdir()
    store = Store(settings.data_dir)
    with store.connection() as db:
        if existing == 'content':
            db.execute("INSERT INTO contents VALUES('existing','我的图片','image',0,NULL,'{}')")
            path = store.image_path('existing')
        else:
            path = store.root / 'saved.png'
            if existing == 'queued-source':
                db.execute("INSERT INTO jobs(id,title,status,created_at,source_snapshot) VALUES('queued','旧画面','pending',0,'saved.png')")
            else:
                column = 'snapshot' if existing == 'snapshot' else 'source_snapshot'
                db.execute(f"UPDATE display_state SET {column}='saved.png'")
    Image.new('RGB', (800, 480), 'white').save(path)
    calls = []
    app = create_app(settings, Display(), lambda stop: calls.append(True))
    with TestClient(app):
        wait_for(lambda: app.state.engine.startup_wallpaper['status'] == 'skipped')
        assert calls == []


@pytest.mark.parametrize('broken', ['missing', 'corrupt', 'wrong-size'])
def test_unusable_library_files_allow_default_initialization(settings, broken):
    store = Store(settings.data_dir)
    with store.connection() as db:
        db.execute("INSERT INTO contents VALUES('broken','损坏图片','image',0,NULL,'{}')")
    if broken == 'corrupt':
        store.image_path('broken').write_bytes(b'not a PNG')
    elif broken == 'wrong-size':
        Image.new('RGB', (20, 20), 'white').save(store.image_path('broken'))
    assert not has_displayable_content(store)
    app = create_app(settings, Display(), lambda stop: default_wallpaper())
    with TestClient(app):
        wait_for(lambda: app.state.store.one("SELECT id FROM jobs WHERE status='done'") is not None)
        assert len(app.state.store.rows('SELECT * FROM contents')) == 2


def test_user_upload_during_download_wins_without_an_orphan_or_extra_refresh(settings):
    started, release = Event(), Event()
    def fetcher(stop):
        started.set()
        assert release.wait(3)
        return default_wallpaper()
    display = Display()
    app = create_app(settings, display, fetcher)
    with TestClient(app) as client:
        try:
            assert started.wait(1)
            assert client.get('/api/health').status_code == 200  # startup isn't blocked by the download.
            response = client.post('/api/contents/image', files={'file': ('own.jpg', image_data(), 'image/jpeg')}, data={'title': '我的照片'})
            assert response.status_code == 201
            release.set()
            wait_for(lambda: app.state.engine.startup_wallpaper['status'] == 'skipped')
            assert [item['title'] for item in client.get('/api/contents').json()] == ['我的照片']
            assert app.state.store.rows('SELECT * FROM jobs') == [] and display.images == []
            assert len(list((settings.data_dir / 'contents').glob('*.png'))) == 1
        finally:
            release.set()


def test_offline_retry_eventually_refreshes(settings, monkeypatch):
    monkeypatch.setattr('app.engine.BING_RETRY_SECONDS', .03)
    calls = []
    def fetcher(stop):
        calls.append(True)
        if len(calls) == 1:
            raise OSError('offline')
        return default_wallpaper()
    app = create_app(settings, Display(), fetcher)
    with TestClient(app):
        wait_for(lambda: app.state.store.one("SELECT id FROM jobs WHERE status='done'") is not None)
        assert len(calls) == 2 and len(app.state.store.rows('SELECT * FROM contents')) == 1


def test_shutdown_cancels_startup_without_saving_or_enqueuing(settings):
    started = Event()
    def fetcher(stop):
        started.set()
        stop.wait(3)
        raise WallpaperCancelled()
    app = create_app(settings, Display(), fetcher)
    before = time.monotonic()
    with TestClient(app):
        assert started.wait(1)
    assert time.monotonic() - before < 2
    assert app.state.store.rows('SELECT * FROM contents') == []
    assert app.state.store.rows('SELECT * FROM jobs') == []


def test_install_is_atomic_and_respects_queue_capacity(settings):
    store = Store(settings.data_dir)
    engine = Engine(store, settings, Display())
    with store.connection() as db:
        db.execute("CREATE TRIGGER reject_startup BEFORE INSERT ON jobs WHEN NEW.purpose='startup_wallpaper' "
                   "BEGIN SELECT RAISE(ABORT,'test failure'); END")
    with pytest.raises(Exception, match='test failure'):
        engine.install_startup_wallpaper(default_wallpaper())
    assert store.rows('SELECT * FROM contents') == [] and list((store.root / 'contents').glob('*.png')) == []
    with store.connection() as db:
        db.execute('DROP TRIGGER reject_startup')
        for index in range(MAX_PENDING):
            db.execute("INSERT INTO jobs(id,title,status,created_at) VALUES(?,?,'pending',0)", (str(index), 'broken'))
    with pytest.raises(ValueError, match='队列已满'):
        engine.install_startup_wallpaper(default_wallpaper())
    assert store.rows('SELECT * FROM contents') == [] and len(store.rows('SELECT * FROM jobs')) == MAX_PENDING
    assert list((store.root / 'contents').glob('*.png')) == []


def test_feature_is_on_by_default_and_can_be_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv('BING_DEFAULT_WALLPAPER', raising=False)
    assert Settings(data_dir=tmp_path).bing_default_wallpaper is True
    monkeypatch.setenv('BING_DEFAULT_WALLPAPER', 'false')
    assert Settings(data_dir=tmp_path).bing_default_wallpaper is False

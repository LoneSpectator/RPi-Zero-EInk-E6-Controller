"""Optional startup UI regression: python -m scripts.browser_wallpaper_smoke."""
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import os
import socket
import time

from PIL import Image
from playwright.sync_api import sync_playwright, expect
import uvicorn

from app.config import Settings
from app.main import create_app
from app.wallpaper import Wallpaper, WallpaperCancelled


expect.set_options(timeout=10000)
with TemporaryDirectory(prefix='photopainter-wallpaper-') as directory:
    started, release = Event(), Event()
    calls = []

    def fetcher(stop):
        calls.append(True)
        started.set()
        while not release.wait(.02):
            if stop.is_set():
                raise WallpaperCancelled()
        return Wallpaper('Bing 今日壁纸 · 2026-11-02', Image.new('RGB', (800, 480), 'blue'),
                         {'source': 'bing', 'mode': 'cover', 'date': '2026-11-02'})

    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    url = f'http://127.0.0.1:{listener.getsockname()[1]}'
    app = create_app(Settings(data_dir=Path(directory), driver='mock', password='',
                              mock_delay=.1, bing_default_wallpaper=True), wallpaper_fetcher=fetcher)
    server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
    thread = Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started:
            if time.monotonic() > deadline:
                raise TimeoutError('Test server did not start')
            time.sleep(.02)
        assert started.wait(1)
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=os.getenv('CHROMIUM_PATH', '/usr/bin/chromium'), args=['--no-sandbox'])
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(url)
            expect(page.locator('#startup-notice')).to_contain_text('正在获取 Bing 今日壁纸')
            expect(page.locator('#connection')).to_have_text('模拟设备已连接')
            assert page.request.get(url+'/api/health').status == 200
            release.set()
            expect(page.locator('#current-title')).to_have_text('Bing 今日壁纸 · 2026-11-02')
            expect(page.locator('#current-screen')).to_have_js_property('naturalWidth', 800)
            expect(page.locator('#startup-notice')).to_be_hidden()
            expect(page.locator('#library-count')).to_have_text('1')
            expect(page.locator('#job-history td.log-type')).to_have_text('默认壁纸')
            expect(page.locator('#job-history .job-state')).to_have_text('已完成')
            page.locator('nav a[data-page="library"]').click()
            expect(page.locator('#library-rows tr')).to_have_count(1)
            page.reload()
            expect(page.locator('#library-rows tr')).to_have_count(1)
            assert calls == [True] and page.request.get(url+'/api/jobs').json()['total'] == 1
            assert not errors, errors
            browser.close()
        print('Startup browser checks passed: nonblocking download notice, automatic save and refresh, current screen, library count, default-wallpaper log and no duplicate on reload.')
    finally:
        release.set()
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()

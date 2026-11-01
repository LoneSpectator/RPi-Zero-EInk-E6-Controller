"""Optional regression: python -m scripts.browser_refresh_smoke (Playwright + Chromium)."""
from io import BytesIO
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


class ControlledDisplay:
    def __init__(self):
        self.started, self.release = Event(), Event()
        self.fail = False

    def display(self, image):
        self.started.set()
        if not self.release.wait(40):
            raise TimeoutError('Browser test did not release display')
        if self.fail:
            raise OSError('Expected failure for browser recovery check')


expect.set_options(timeout=10000)
with TemporaryDirectory(prefix='photopainter-refresh-') as directory:
    display = ControlledDisplay()
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    url = f'http://127.0.0.1:{listener.getsockname()[1]}'
    app = create_app(Settings(bing_default_wallpaper=False, data_dir=Path(directory), driver='mock', password=''), display=display)
    server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
    thread = Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started:
            if time.monotonic() > deadline:
                raise TimeoutError('Test server did not start')
            time.sleep(.02)
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=os.getenv('CHROMIUM_PATH', '/usr/bin/chromium'), args=['--no-sandbox'])
            page = browser.new_page()
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(url + '/#image')
            data = BytesIO(); Image.new('RGB', (100, 100), 'red').save(data, 'PNG')
            page.locator('#image-file').set_input_files({'name': 'test.png', 'mimeType': 'image/png', 'buffer': data.getvalue()})
            page.locator('#image-preview').click()
            expect(page.locator('#image-display')).to_be_enabled()
            page.locator('nav a[data-page="text"]').click()
            page.locator('#text-title').fill('刷新时仍可保存')
            page.locator('#text-preview').click()
            expect(page.locator('#text-display')).to_be_enabled()
            page.locator('nav a[data-page="image"]').click()
            page.locator('#image-display').click()
            expect(page.locator('#image-display')).to_be_disabled()
            assert display.started.wait(2)
            expect(page.locator('#refresh-notice')).to_contain_text('屏幕正在刷新')
            expect(page.locator('#header-screen')).to_have_text('屏幕刷新中')
            expect(page.locator('#header-battery')).to_have_text('—%')

            # Re-rendering the library or regenerating either preview must not unlock display.
            page.locator('nav a[data-page="library"]').click()
            expect(page.locator('[data-display]')).to_be_disabled()
            page.locator('#library-search').fill('test')
            expect(page.locator('[data-display]')).to_be_disabled()
            for kind in ('image', 'text'):
                page.locator(f'nav a[data-page="{kind}"]').click()
                page.locator(f'#{kind}-preview').click()
                expect(page.locator(f'#{kind}-save')).to_be_enabled()
                expect(page.locator(f'#{kind}-display')).to_be_disabled()
            page.locator('#text-save').click()
            expect(page.locator('#toast')).to_contain_text('内容已保存')
            assert len(page.request.get(url+'/api/status').json()['jobs']) == 1

            # A second tab sees the same gate; direct API requests cannot bypass it.
            other = browser.new_page()
            other.goto(url+'/#library')
            expect(other.locator('[data-display]')).to_have_count(2)
            for button in other.locator('[data-display]').all():
                expect(button).to_be_disabled()
            content = other.request.get(url+'/api/contents').json()[0]
            assert other.request.post(url+f"/api/contents/{content['id']}/display").status == 409
            other.close()

            display.release.set()
            expect(page.locator('#text-display')).to_be_enabled()
            # Unknown status blocks refresh, then a successful reconnect restores it.
            page.route('**/api/status', lambda route: route.abort())
            page.evaluate('refreshStatus()')
            expect(page.locator('#text-display')).to_be_disabled()
            expect(page.locator('#header-screen')).to_have_text('屏幕状态未知')
            expect(page.locator('#connection')).to_have_class('connection status-error')
            assert page.locator('#connection').evaluate('(el)=>getComputedStyle(el).color') == 'rgb(198, 40, 40)'
            expect(page.locator('#header-battery')).to_have_text('—%')
            expect(page.locator('#text-save')).to_be_enabled()
            page.unroute('**/api/status')
            page.evaluate('refreshStatus()')
            expect(page.locator('#text-display')).to_be_enabled()

            display.fail = True; display.release.clear(); display.started.clear()
            page.locator('#text-display').click()
            assert display.started.wait(2)
            expect(page.locator('#text-display')).to_be_disabled()
            display.release.set()
            expect(page.locator('#text-display')).to_be_enabled()
            page.locator('nav a[data-page="overview"]').click()
            expect(page.locator('#header-screen')).to_have_text('屏幕刷新失败')
            assert not errors, errors
            browser.close()
        print('Refresh browser checks passed: all entry points, preview/save while busy, second tab, reconnect and failure recovery.')
    finally:
        display.release.set()
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()

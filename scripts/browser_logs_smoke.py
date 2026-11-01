"""Optional software UI regression: python -m scripts.browser_logs_smoke."""
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from zoneinfo import ZoneInfo
import os
import socket
import time

from playwright.sync_api import sync_playwright, expect
import uvicorn

from app.config import Settings
from app.main import create_app
from app.store import now_ts


expect.set_options(timeout=10000)
with TemporaryDirectory(prefix='photopainter-logs-') as directory:
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    url = f'http://127.0.0.1:{listener.getsockname()[1]}'
    app = create_app(Settings(bing_default_wallpaper=False, data_dir=Path(directory), driver='mock', password=''))
    server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
    thread = Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started:
            if time.monotonic() > deadline:
                raise TimeoutError('Test server did not start')
            time.sleep(.02)
        anchor = now_ts()
        with app.state.store.connection() as db:
            for index in range(105):
                stamp = anchor - (4 * 86400 if index < 20 else 12 * 3600) + index
                error = '<script>window.logXss=true</script> GPIO error' if index == 104 else None
                db.execute('INSERT INTO jobs(id,title,status,error,created_at,finished_at,scheduled_for,purpose) '
                           'VALUES(?,?,?,?,?,?,?,?)',
                           (f'log-{index:03}', f'刷新 <测试> {index:03}', 'failed' if error else 'done', error,
                            stamp, stamp + 1, stamp if index % 3 == 1 else None,
                            'battery_warning' if index % 3 == 2 else 'display'))
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=os.getenv('CHROMIUM_PATH', '/usr/bin/chromium'), args=['--no-sandbox'])
            page = browser.new_page(viewport={'width':1440,'height':1100}, timezone_id='America/New_York', reduced_motion='reduce')
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(url)
            expect(page.locator('#job-history tbody tr')).to_have_count(5)
            expect(page.locator('#job-history th')).to_have_text(['日期时间','刷新内容','类型','状态'])
            page.locator('#job-history details summary').click()
            page.evaluate('refreshStatus()')
            assert page.locator('#job-history details').get_attribute('open') is not None
            assert page.locator('nav a').evaluate_all('(els)=>els.slice(-2).map(el=>el.dataset.page)') == ['schedule','logs']
            page.locator('.history-card a[href="#logs"]').click()
            expect(page.locator('#logs-list tbody tr')).to_have_count(20)
            expect(page.locator('#logs-retention')).to_have_value('7')
            expect(page.locator('#logs-page-label')).to_have_text('第 1 / 6 页')
            expect(page.locator('#logs-previous')).to_be_disabled()
            first_ids = page.locator('#logs-list tbody tr').evaluate_all('(els)=>els.map(el=>el.dataset.jobId)')
            first_time = page.locator('#logs-list time').first.inner_text()
            expected = datetime.fromtimestamp(anchor - 12 * 3600 + 105, ZoneInfo('Asia/Shanghai'))
            assert expected.strftime('%Y-%m-%d') in first_time and expected.strftime('%H:%M:%S') in first_time
            page.locator('#logs-next').click()
            expect(page.locator('#logs-page-label')).to_have_text('第 2 / 6 页')
            assert not set(first_ids) & set(page.locator('#logs-list tbody tr').evaluate_all('(els)=>els.map(el=>el.dataset.jobId)'))
            page.locator('#logs-page-size').select_option('50')
            expect(page.locator('#logs-list tbody tr')).to_have_count(50)
            expect(page.locator('#logs-page-label')).to_have_text('第 1 / 3 页')
            page.locator('#logs-next').click()
            expect(page.locator('#logs-page-label')).to_have_text('第 2 / 3 页')
            page.locator('#logs-next').click()
            expect(page.locator('#logs-list tbody tr')).to_have_count(5)
            expect(page.locator('#logs-next')).to_be_disabled()
            page.locator('#logs-page-size').select_option('100')
            expect(page.locator('#logs-list tbody tr')).to_have_count(100)
            page.locator('#logs-next').click()
            expect(page.locator('#logs-list tbody tr')).to_have_count(5)
            page.locator('#logs-previous').click()
            expect(page.locator('#logs-list tbody tr')).to_have_count(100)
            failure = page.locator('#logs-list [data-job-id="log-104"] details')
            failure.locator('summary').click()
            expect(failure.locator('p')).to_have_text('<script>window.logXss=true</script> GPIO error')
            assert page.locator('#logs-list script').count() == 0
            assert page.evaluate('typeof window.logXss') == 'undefined'
            with app.state.store.connection() as db:
                db.execute("INSERT INTO jobs(id,title,status,created_at,finished_at) VALUES('new','新增刷新','done',?,?)", (anchor,anchor))
            page.evaluate('refreshLogs()')
            assert failure.get_attribute('open') is not None  # live updates preserve expanded errors.
            page.locator('#logs-retention').fill('2')
            page.evaluate('refreshLogs()')
            expect(page.locator('#logs-retention')).to_have_value('2')
            assert page.request.get(url+'/api/settings/logs').json()['retention_days'] == 7
            page.locator('#logs-save').click()
            expect(page.locator('#toast')).to_have_text('日志保留天数已保存')
            expect(page.locator('#logs-list tbody tr')).to_have_count(86)
            expect(page.locator('#logs-page-label')).to_have_text('第 1 / 1 页')
            assert len(app.state.store.rows('SELECT * FROM jobs')) == 86
            page.reload()
            expect(page.locator('#logs-retention')).to_have_value('2')
            expect(page.locator('#logs-list tbody tr')).to_have_count(20)
            expect(page.locator('#logs-summary')).to_have_text('共 86 条记录')
            assert page.locator('#logs-list tbody tr').first.bounding_box()['height'] < 50
            page.screenshot(path='/tmp/photopainter-logs.png', full_page=True)
            for width in (1440,1024,760,390,320):
                page.set_viewport_size({'width':width,'height':1100})
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),width
                assert page.locator('#logs-list table').evaluate('(el)=>el.scrollWidth<=el.clientWidth'),width
            page.set_viewport_size({'width':390,'height':1100})
            page.screenshot(path='/tmp/photopainter-logs-mobile.png', full_page=True)
            with app.state.store.connection() as db:
                db.execute('DELETE FROM jobs')
            page.evaluate('refreshLogs(999)')
            expect(page.locator('#logs-list')).to_have_text('还没有刷新记录')
            expect(page.locator('#logs-page-label')).to_have_text('第 1 / 1 页')
            expect(page.locator('#logs-previous')).to_be_disabled()
            expect(page.locator('#logs-next')).to_be_disabled()
            assert not errors,errors
            browser.close()
        print('Log browser checks passed: recent 5, compact columns, server timezone, 20/50/100 pagination, live error expansion, escaped text, retention save/pruning/persistence, empty state and mobile layout.')
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()

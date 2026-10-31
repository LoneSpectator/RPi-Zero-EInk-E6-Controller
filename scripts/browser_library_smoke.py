"""Optional regression: python -m scripts.browser_library_smoke (Playwright + Chromium)."""
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import os
import socket
import time

from PIL import Image
from playwright.sync_api import sync_playwright, expect
import uvicorn

from app.config import Settings
from app.main import create_app


expect.set_options(timeout=10000)
with TemporaryDirectory(prefix='photopainter-library-') as directory:
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    url = f'http://127.0.0.1:{listener.getsockname()[1]}'
    app = create_app(Settings(data_dir=Path(directory), driver='mock', password='', mock_delay=.01))
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
            page = browser.new_page(viewport={'width':1440,'height':1000},reduced_motion='reduce')
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            data = BytesIO(); Image.new('RGB',(100,100),'blue').save(data,'PNG')
            images = []
            for title in ['风景 <测试>', '另一张照片']:
                response = page.request.post(url+'/api/contents/image', multipart={'file':{'name':'test.png','mimeType':'image/png','buffer':data.getvalue()},'title':title})
                assert response.status == 201,response.text()
                images.append(response.json())
            response = page.request.post(url+'/api/contents/text', data={'title':'很长的文字内容名称'*8,'paragraphs':[{'runs':[{'text':'测试列表'}]}]})
            assert response.status == 201,response.text()
            text = response.json()
            page.goto(url+'/#library')
            expect(page.locator('#library-rows tr')).to_have_count(3)
            expect(page.locator('#library-delete')).to_be_hidden()
            expect(page.locator('.library-actions a')).to_have_text('＋ 创建内容')

            page.locator(f'[data-select-content="{images[0]["id"]}"]').check()
            assert page.locator('#library-select-all').evaluate('(el)=>el.indeterminate')
            expect(page.locator('#library-delete')).to_have_text('删除（1）')
            create = page.locator('.library-actions a').bounding_box()
            delete = page.locator('#library-delete').bounding_box()
            assert delete['x'] >= create['x']+create['width']
            page.locator('#library-delete').click()
            expect(page.locator('#delete-dialog')).to_be_visible()
            expect(page.locator('#delete-names')).to_contain_text('风景 <测试>')
            assert page.locator('#delete-names script').count() == 0
            page.locator('#cancel-delete').click()
            assert len(page.request.get(url+'/api/contents').json()) == 3

            # Filtering never leaves invisible content selected for deletion.
            page.locator('#library-filter').select_option('text')
            expect(page.locator('#library-rows tr')).to_have_count(1)
            expect(page.locator('#library-delete')).to_be_hidden()
            page.locator('#library-select-all').check()
            expect(page.locator('#library-delete')).to_have_text('删除（1）')
            page.locator('#library-filter').select_option('all')
            assert page.locator('#library-select-all').evaluate('(el)=>el.indeterminate')
            page.locator('#library-select-all').check()
            expect(page.locator('#library-delete')).to_have_text('删除（3）')
            page.locator('#library-search').fill('风景')
            expect(page.locator('#library-rows tr')).to_have_count(1)
            expect(page.locator('#library-delete')).to_have_text('删除（1）')
            page.locator('#library-search').fill('')
            page.locator('#library-select-all').check();page.locator('#library-select-all').uncheck()

            for image in images:
                page.locator(f'[data-select-content="{image["id"]}"]').check()
            expect(page.locator('#library-delete')).to_have_text('删除（2）')
            for width in (1440,1024,760,390,320):
                page.set_viewport_size({'width':width,'height':1000})
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),width
                assert page.locator('.library-table').evaluate('(el)=>el.scrollWidth<=el.clientWidth'),width
            page.set_viewport_size({'width':1440,'height':1000})
            page.screenshot(path='/tmp/photopainter-library-list.png',full_page=True)
            page.locator('#library-delete').click()
            expect(page.locator('#delete-message')).to_contain_text('2 份内容')
            page.locator('#confirm-delete').click()
            expect(page.locator('#delete-dialog')).not_to_be_visible()
            expect(page.locator('#library-rows tr')).to_have_count(1)
            expect(page.locator('#library-delete')).to_be_hidden()

            # A schedule reference blocks the batch and leaves confirmation visible.
            schedule = page.request.post(url+'/api/schedules',data={'title':'引用','content_id':text['id'],'kind':'cron','cron':'0 8 * * *','enabled':False}).json()
            page.locator('#library-select-all').check();page.locator('#library-delete').click()
            page.locator('#confirm-delete').click()
            expect(page.locator('#delete-error')).to_contain_text('未删除任何内容')
            expect(page.locator('#delete-dialog')).to_be_visible()
            assert len(page.request.get(url+'/api/contents').json()) == 1
            page.locator('#cancel-delete').click()
            assert page.request.delete(url+'/api/schedules/'+schedule['id']).status == 200
            page.set_viewport_size({'width':390,'height':1000})
            page.screenshot(path='/tmp/photopainter-library-list-mobile.png',full_page=True)
            page.locator('#library-delete').click();page.locator('#confirm-delete').click()
            expect(page.locator('#library-empty')).to_be_visible()
            expect(page.locator('#library-count')).to_have_text('0')
            expect(page.locator('#library-delete')).to_be_hidden()
            assert page.request.get(url+'/api/contents').json() == []
            assert not errors,errors
            browser.close()
        print('Library browser checks passed: list layout, selection and filtering, cancel/confirm batch deletion, schedule protection, empty state and mobile layout.')
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()

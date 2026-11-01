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
    app = create_app(Settings(bing_default_wallpaper=False, data_dir=Path(directory), driver='mock', password='', mock_delay=.01))
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
            # A blank name must allow preview/save and use the first nonblank line,
            # including text spanning differently styled runs and a BR line break.
            page.goto(url+'/#text')
            page.locator('#text-title').fill('  ')
            automatic_name = '很长的文字内容名称'*8
            page.locator('#text-editor').evaluate('(el, name)=>{el.innerHTML=`<p>&nbsp; </p><p> 很长的文字 <b>${name.slice(5)}</b><br>第二行</p>`}', automatic_name)
            page.locator('#text-preview').click()
            expect(page.locator('#text-save')).to_be_enabled()
            page.locator('#text-save').click()
            expect(page.locator('#toast')).to_have_text('内容已保存，可在内容库和排程中使用')
            text = next(c for c in page.request.get(url+'/api/contents').json() if c['kind']=='text')
            assert text['title'] == automatic_name
            page.goto(url+'/#library')
            expect(page.locator('#library-rows tr')).to_have_count(3)
            expect(page.locator('#library-delete')).to_be_hidden()
            expect(page.locator('#page-library a.button')).to_have_count(0)
            headings = page.locator('.library-table th').evaluate_all('(cells)=>cells.map(c=>({align:getComputedStyle(c).textAlign,size:parseFloat(getComputedStyle(c).fontSize)}))')
            body_size = page.locator('.library-details strong').first.evaluate('(el)=>parseFloat(getComputedStyle(el).fontSize)')
            assert all(h['align']=='center' and h['size']>=body_size for h in headings)

            # Images have only their title; text exposes a compact, complete
            # second line and an accessible full-text tooltip.
            image_details = page.locator(f'[data-content-id="{images[0]["id"]}"] .library-details')
            expect(image_details).to_have_text(images[0]['title'])
            summary = page.locator(f'[data-content-id="{text["id"]}"] .library-text-preview')
            expected_body = automatic_name + '第二行'
            expect(summary).to_have_text(expected_body)
            assert summary.evaluate('(el)=>el.scrollWidth>el.clientWidth&&getComputedStyle(el).textOverflow==="ellipsis"')
            summary.hover()
            expect(page.locator('#library-text-tooltip')).to_have_text(expected_body)
            page.locator('#library-text-tooltip').hover()
            page.wait_for_timeout(250)
            expect(page.locator('#library-text-tooltip')).to_be_visible()
            page.locator('#library-search').click()
            expect(page.locator('#library-text-tooltip')).to_be_hidden()
            summary.focus()
            expect(summary).to_have_attribute('aria-describedby', 'library-text-tooltip')
            expect(page.locator('#library-text-tooltip')).to_be_visible()

            # Enlarging either thumbnail never queues a screen refresh. Image
            # clicks keep it open; blank space, Escape and the close button exit.
            for item in (images[0], text):
                thumbnail = page.locator(f'[data-preview="{item["id"]}"]')
                thumbnail.click()
                expect(page.locator('#content-preview-dialog')).to_be_visible()
                expect(page.locator('#content-preview-image')).to_have_js_property('naturalWidth', 800)
                page.locator('#content-preview-image').click()
                expect(page.locator('#content-preview-dialog')).to_be_visible()
                page.mouse.click(8, 8)
                expect(page.locator('#content-preview-dialog')).not_to_be_visible()
                thumbnail.click()
                page.keyboard.press('Escape')
                expect(page.locator('#content-preview-dialog')).not_to_be_visible()
                thumbnail.click()
                page.locator('#close-content-preview').click()
                expect(page.locator('#content-preview-dialog')).not_to_be_visible()
            assert page.request.get(url+'/api/jobs').json()['total'] == 0

            # Cancel, whitespace validation, then rename without losing selection.
            rename = page.locator(f'[data-rename="{images[0]["id"]}"]')
            rename.click()
            expect(page.locator('#rename-title')).to_have_value(images[0]['title'])
            page.locator('#rename-title').fill('取消这次修改')
            page.locator('#cancel-rename').click()
            assert page.request.get(url+'/api/contents').json()[2]['title'] == images[0]['title']
            page.locator(f'[data-select-content="{images[0]["id"]}"]').check()
            rename.click()
            page.locator('#rename-title').fill('  ')
            page.locator('#confirm-rename').click()
            expect(page.locator('#rename-error')).to_have_text('内容名称不能为空')
            page.locator('#rename-title').fill('新风景 <测试>')
            page.locator('#rename-title').press('Enter')
            expect(page.locator('#rename-dialog')).not_to_be_visible()
            expect(page.locator(f'[data-content-id="{images[0]["id"]}"] .library-details strong')).to_have_text('新风景 <测试>')
            expect(page.locator(f'[data-select-content="{images[0]["id"]}"]')).to_be_checked()

            # The cached text preview follows a library rename without a new entry.
            page.locator(f'[data-rename="{text["id"]}"]').click()
            page.locator('#rename-title').fill(automatic_name+'改名')
            page.locator('#confirm-rename').click()
            expect(page.locator('#rename-dialog')).not_to_be_visible()
            page.goto(url+'/#text')
            expect(page.locator('#text-title')).to_have_value(automatic_name+'改名')
            page.locator('#text-save').click()
            expect(page.locator('#toast')).to_have_text('内容已保存，可在内容库和排程中使用')
            assert len(page.request.get(url+'/api/contents').json()) == 3
            page.goto(url+'/#library')

            page.locator(f'[data-select-content="{images[0]["id"]}"]').check()
            assert page.locator('#library-select-all').evaluate('(el)=>el.indeterminate')
            expect(page.locator('#library-delete')).to_have_text('删除（1）')
            filter = page.locator('#library-filter').bounding_box()
            delete = page.locator('#library-delete').bounding_box()
            assert delete['x'] >= filter['x']+filter['width'] and abs(delete['y']-filter['y'])<3
            page.locator('#library-delete').click()
            expect(page.locator('#delete-dialog')).to_be_visible()
            expect(page.locator('#delete-names')).to_contain_text('新风景 <测试>')
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
        print('Library browser checks passed: thumbnail enlargement and dismissal, complete compact text and hover/focus tooltip, title-only images, automatic names, rename, cached previews, selection, deletion, schedule protection and mobile layout.')
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()

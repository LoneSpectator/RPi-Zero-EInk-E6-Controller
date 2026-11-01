"""Optional software UI regression: python -m scripts.browser_alignment_smoke."""
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import base64
import os
import socket
import time

from PIL import Image, ImageChops
from playwright.sync_api import sync_playwright, expect
import uvicorn

from app.config import Settings
from app.main import create_app


expect.set_options(timeout=10000)
with TemporaryDirectory(prefix='photopainter-alignment-') as directory:
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    url = f'http://127.0.0.1:{listener.getsockname()[1]}'
    app = create_app(Settings(bing_default_wallpaper=False, data_dir=Path(directory), driver='mock', password=''))
    server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
    thread = Thread(target=server.run, kwargs={'sockets':[listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started:
            if time.monotonic() > deadline:
                raise TimeoutError('Test server did not start')
            time.sleep(.02)
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=os.getenv('CHROMIUM_PATH','/usr/bin/chromium'), args=['--no-sandbox'])
            page = browser.new_page(viewport={'width':1440,'height':1100}, reduced_motion='reduce')
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(url+'/#text')
            buttons = page.locator('.alignment-button')
            assert buttons.evaluate_all('(els)=>els.map(el=>el.title)') == ['行左对齐','行居中','行两端对齐','居上','居中','居下']
            expect(buttons.locator('svg')).to_have_count(6)
            def preview():
                with page.expect_response(lambda r:r.url.endswith('/api/preview/text') and r.request.method=='POST') as pending:
                    page.locator('#text-preview').click()
                response = pending.value
                assert response.status == 200,response.text()
                expect(page.locator('#text-save')).to_be_enabled()
                expect(page.locator('#text-result')).to_have_js_property('naturalWidth',800)
                encoded = page.locator('#text-result').evaluate('''img=>{
                    const canvas=document.createElement('canvas');canvas.width=img.naturalWidth;canvas.height=img.naturalHeight;
                    canvas.getContext('2d').drawImage(img,0,0);return canvas.toDataURL('image/png').split(',')[1];
                }''')
                data = base64.b64decode(encoded)
                return response.request.post_data_json, Image.open(BytesIO(data)).convert('RGB')

            # Verify the actual document sent to Pillow, including mixed italic
            # styles, rather than relying on how the browser paints the sample.
            payload, sample_image = preview()
            runs = payload['paragraphs'][0]['runs']
            assert payload['title'] == '' and payload['background'] == '#ffffff'
            assert payload['vertical_align'] == 'top' and payload['padding'] == 24
            assert payload['paragraphs'][0]['align'] == 'center'
            assert [run['text'] for run in runs] == ['五', '颜', '六', '色', '\u3000黑'], runs
            assert [run['color'] for run in runs] == ['#ff0000', '#ffff00', '#0000ff', '#00ff00', '#000000'], runs
            assert all(run['font'] == 'source-sans' and run['size'] == 32 and run['bold'] for run in runs), runs
            assert all(run['italic'] for run in runs[:4]) and not runs[-1]['italic'], runs
            assert set(sample_image.get_flattened_data()) == {(255,255,255), (255,0,0), (255,255,0), (0,0,255), (0,255,0), (0,0,0)}
            page.screenshot(path='/tmp/photopainter-text-default.png',full_page=True)

            body = '和光同尘，日子缓缓。'*4
            page.locator('#text-editor').fill(body)
            page.locator('#text-editor').click()
            page.keyboard.press('Control+A')
            for command, alignment in [('justifyLeft','left'),('justifyCenter','center'),('justifyFull','justify')]:
                page.locator(f'[data-command="{command}"]').click()
                expect(page.locator(f'[data-command="{command}"]')).to_have_attribute('aria-pressed','true')
                expect(page.locator('#text-save')).to_be_disabled()
                payload, image = preview()
                assert all(p['align']==alignment for p in payload['paragraphs']),payload
                assert ''.join(run['text'] for p in payload['paragraphs'] for run in p['runs']) == body,payload
            boxes = []
            for alignment in ('top','center','bottom'):
                page.locator(f'.toolbar [data-vertical-align="{alignment}"]').click()
                expect(page.locator(f'.toolbar [data-vertical-align="{alignment}"]')).to_have_attribute('aria-pressed','true')
                expect(page.locator('#text-save')).to_be_disabled()
                payload, image = preview()
                assert payload['vertical_align'] == alignment
                assert all(p['align']=='justify' for p in payload['paragraphs']),payload
                assert ''.join(run['text'] for p in payload['paragraphs'] for run in p['runs']) == body,payload
                boxes.append(ImageChops.difference(image,Image.new('RGB',image.size,'white')).getbbox())
            assert boxes[0][1]<boxes[1][1]<boxes[2][1],boxes
            with page.expect_response(lambda r:r.url.endswith('/api/contents/text') and r.request.method=='POST') as pending:
                page.locator('#text-save').click()
            saved = pending.value
            assert saved.status == 201,saved.text()
            expect(page.locator('#toast')).to_contain_text('内容已保存')
            item = saved.json()
            stored_image = Image.open(BytesIO(page.request.get(url+'/api/contents/'+item['id']+'/image').body())).convert('RGB')
            assert stored_image.tobytes() == image.tobytes()
            page.locator('.toolbar').screenshot(path='/tmp/photopainter-alignment-toolbar.png')
            page.screenshot(path='/tmp/photopainter-text-alignment.png',full_page=True)
            for width in (1440,1024,760,390,320):
                page.set_viewport_size({'width':width,'height':1100})
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),width
            page.set_viewport_size({'width':390,'height':1100})
            page.screenshot(path='/tmp/photopainter-text-alignment-mobile.png',full_page=True)
            assert not errors,errors
            browser.close()
        print('Alignment browser checks passed: default colored sample and six-color pixels, SVG tooltips, horizontal commands, vertical positioning, style preservation, preview invalidation, saved pixels and mobile layout.')
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()

from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image

from app.config import Settings
from app.main import create_app


def test_library_returns_complete_compact_text_across_styles_and_paragraphs(tmp_path):
    document = {'title': '正文标题', 'paragraphs': [
        {'runs': [{'text': ' 五\t颜\u3000', 'color': '#ff0000'}, {'text': '六 色\n黑\u00a0', 'bold': True}]},
        {'runs': [{'text': '第二\r\n行 <全文> & 结尾'}]},
    ]}
    with TestClient(create_app(Settings(data_dir=tmp_path, driver='mock', password=''))) as client:
        created = client.post('/api/contents/text', json=document)
        assert created.status_code == 201, created.text
        expected = '五颜六色黑第二行<全文>&结尾'
        assert created.json()['text_content'] == expected
        item = client.get('/api/contents').json()[0]
        assert item['title'] == '正文标题' and item['text_content'] == expected
        assert 'payload' not in item
        renamed = client.patch('/api/contents/' + item['id'], json={'title': '新标题'})
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()['text_content'] == expected
        assert client.get('/api/contents').json()[0]['text_content'] == expected


def test_library_does_not_truncate_long_text_or_add_a_text_description_to_images(tmp_path):
    body = '长正文' * 80
    data = BytesIO()
    Image.new('RGB', (100, 100), 'red').save(data, 'PNG')
    with TestClient(create_app(Settings(data_dir=tmp_path, driver='mock', password=''))) as client:
        created = client.post('/api/contents/text', json={'paragraphs': [{'runs': [{'text': body, 'size': 12}]}]})
        assert created.status_code == 201, created.text
        image = client.post('/api/contents/image', files={'file': ('image.png', data.getvalue(), 'image/png')}, data={'title': '照片'})
        assert image.status_code == 201, image.text
        items = client.get('/api/contents').json()
        text = next(item for item in items if item['kind'] == 'text')
        assert text['title'] == body[:120] and text['text_content'] == body
        image = next(item for item in items if item['kind'] == 'image')
        assert image['title'] == '照片' and 'text_content' not in image and 'payload' not in image

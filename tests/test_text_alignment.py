from io import BytesIO
import json

from fastapi.testclient import TestClient
from PIL import Image, ImageChops
import pytest

from app.config import Settings
from app.main import create_app
from app.models import TextContent
from app.render import render_text


def ink_box(image):
    return ImageChops.difference(image, Image.new('RGB', image.size, 'white')).getbbox()


def test_vertical_alignment_moves_the_whole_styled_block_without_changing_pixels():
    paragraphs = [
        {'align': 'left', 'runs': [{'text': '山有木兮', 'size': 32}]},
        {'align': 'center', 'runs': [{'text': '木有枝', 'size': 48, 'bold': True, 'italic': True}]},
    ]
    images = [render_text(TextContent(paragraphs=paragraphs, vertical_align=align)) for align in ('top', 'center', 'bottom')]
    boxes = [ink_box(image) for image in images]
    assert boxes[0][1] < boxes[1][1] < boxes[2][1]
    assert abs((boxes[1][1] + boxes[1][3]) / 2 - 240) < 16
    assert 24 <= 480 - boxes[2][3] < 48
    assert [image.crop(box).tobytes() for image, box in zip(images, boxes)] == [images[0].crop(boxes[0]).tobytes()] * 3
    assert all(box[0] == boxes[0][0] and box[2] == boxes[0][2] for box in boxes)


def test_justification_stretches_wrapped_lines_but_not_final_or_manual_breaks(monkeypatch):
    def tile(run, char):
        return Image.new('RGBA', (110, 8), (0, 0, 0, 255)), 110, 10
    monkeypatch.setattr('app.render.glyph', tile)
    image = render_text(TextContent(paragraphs=[{'align': 'justify', 'runs': [{'text': 'ABCDEFGH'}]}]))
    assert image.getpixel((24, 24)) == (0, 0, 0)
    assert image.getpixel((775, 24)) == (0, 0, 0)  # both ends of automatically wrapped line.
    assert image.getpixel((134, 24)) == (255, 255, 255)  # added gap after first glyph.
    assert image.getpixel((243, 34)) == (0, 0, 0)
    assert image.getpixel((244, 34)) == (255, 255, 255)  # short final line is not stretched.
    manual = render_text(TextContent(paragraphs=[{'align': 'justify', 'runs': [{'text': 'AB\nCD'}]}]))
    assert manual.getpixel((243, 24)) == (0, 0, 0)
    assert manual.getpixel((244, 24)) == (255, 255, 255)


def test_justification_expands_word_spaces_and_preserves_latin_letter_spacing(monkeypatch):
    def tile(run, char):
        width = 20 if char == ' ' else 100
        return Image.new('RGBA', (width, 8), (0, 0, 0, 0 if char == ' ' else 255)), width, 10
    monkeypatch.setattr('app.render.glyph', tile)
    image = render_text(TextContent(paragraphs=[{'align': 'justify', 'runs': [{'text': 'AA BB CC DD EE'}]}]))
    assert image.getpixel((124, 24)) == (0, 0, 0)  # letters within AA remain adjacent.
    assert image.getpixel((270, 24)) == (255, 255, 255)  # expanded inter-word space.
    assert image.getpixel((775, 24)) == (0, 0, 0)


@pytest.mark.parametrize('vertical', ['top', 'center', 'bottom'])
def test_preview_and_saved_pixels_honor_both_alignment_axes(tmp_path, vertical):
    app = create_app(Settings(data_dir=tmp_path, driver='mock', password=''))
    with TestClient(app) as client:
        doc = {'vertical_align': vertical, 'paragraphs': [{'align': 'justify', 'runs': [{'text': '和光同尘，日子缓缓。' * 4}]}]}
        preview = client.post('/api/preview/text', json=doc)
        assert preview.status_code == 200, preview.text
        saved = client.post('/api/contents/text', json=doc)
        assert saved.status_code == 201, saved.text
        item = saved.json()
        payload = json.loads(item['payload'])
        assert payload['vertical_align'] == vertical and payload['paragraphs'][0]['align'] == 'justify'
        assert client.get('/api/contents/' + item['id'] + '/image').content == preview.content
        assert ink_box(Image.open(BytesIO(preview.content))) is not None
        doc['paragraphs'][0]['runs'][0]['text'] = '太多文字\n' * 30
        assert client.post('/api/preview/text', json=doc).status_code == 422


def test_old_right_alignment_documents_remain_supported():
    paragraphs = [{'runs': [{'text': '保留右对齐', 'size': 32}]}]
    left = render_text(TextContent(paragraphs=paragraphs))
    paragraphs[0]['align'] = 'right'
    right = render_text(TextContent(paragraphs=paragraphs))
    assert ink_box(right)[0] > ink_box(left)[0]


@pytest.mark.parametrize('value', ['middle', 'baseline', None])
def test_invalid_vertical_alignment_is_rejected(value):
    with pytest.raises(ValueError):
        TextContent(vertical_align=value, paragraphs=[{'runs': [{'text': '正文'}]}])

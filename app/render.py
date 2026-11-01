"""One rendering path for both previews and hardware; no browser HTML on server."""
from functools import lru_cache
from io import BytesIO
from pathlib import Path
import os
from PIL import Image, ImageOps, ImageFont, ImageDraw, UnidentifiedImageError
from .models import TextContent

WIDTH, HEIGHT = 800, 480
COLORS = [(0, 0, 0), (255, 255, 255), (255, 255, 0), (255, 0, 0), (0, 0, 255), (0, 255, 0)]
Image.MAX_IMAGE_PIXELS = 20_000_000
FONT_FILES = {
    'heiti': '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
    'songti': '/usr/share/fonts/truetype/arphic/uming.ttc',
    'kaiti': '/usr/share/fonts/truetype/arphic/ukai.ttc',
    'source-sans': '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    'source-serif': '/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc',
}


def battery_warning(image, low):
    if not low:
        return image
    image = image.copy()
    ImageDraw.Draw(image).rectangle((0, 0, image.width - 1, image.height - 1), outline='#ff0000', width=5)
    return image


def font_path(name):
    return Path(os.getenv('FONT_' + name.upper().replace('-', '_'), FONT_FILES[name]))


def font_status():
    return {name: {'available': font_path(name).is_file()} for name in FONT_FILES}


@lru_cache(maxsize=32)
def font(name, size):
    path = font_path(name)
    if not path.is_file():
        raise ValueError(f'缺少字体 {name}，请按 README 安装字体或配置 FONT_* 路径')
    # Noto CJK collection face 2 is Simplified Chinese; Arphic and WQY face 0.
    index = 2 if name.startswith('source-') and path.suffix == '.ttc' else 0
    return ImageFont.truetype(str(path), size, index=index)


def quantize(image, dither=True):
    palette = Image.new('P', (1, 1))
    palette.putpalette(sum((list(c) for c in COLORS), []) + [0, 0, 0] * 250)
    return image.convert('RGB').quantize(palette=palette, dither=(Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE)).convert('RGB')


def png(image):
    out = BytesIO()
    image.save(out, format='PNG')
    return out.getvalue()


def render_image(data: bytes, mode: str):
    if mode not in ('contain', 'cover'):
        raise ValueError('缩放方式必须为 contain 或 cover')
    try:
        with Image.open(BytesIO(data)) as original:
            if original.format not in ('JPEG', 'PNG'):
                raise ValueError('仅支持 JPG / PNG 图片')
            if original.width * original.height > Image.MAX_IMAGE_PIXELS:
                raise ValueError('图片不能超过 2000 万像素')
            original.load()
            image = ImageOps.exif_transpose(original)
            # Resize before allocating RGBA compositing layers: Pi Zero 2 W has 512 MiB.
            if mode == 'cover':
                fitted = ImageOps.fit(image, (WIDTH, HEIGHT), Image.Resampling.LANCZOS, centering=(.5, .5))
            else:
                fitted = ImageOps.contain(image, (WIDTH, HEIGHT), Image.Resampling.LANCZOS)
            rgba = fitted.convert('RGBA')
            fitted = Image.alpha_composite(Image.new('RGBA', rgba.size, 'white'), rgba).convert('RGB')
            image = Image.new('RGB', (WIDTH, HEIGHT), 'white')
            image.paste(fitted, ((WIDTH-fitted.width)//2, (HEIGHT-fitted.height)//2))
            return quantize(image)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError('无效或过大的图片') from exc


def glyph(run, char):
    face = font(run.font, run.size)
    stroke = max(1, run.size // 28) if run.bold else 0
    advance = max(1, round(face.getlength(char))) + stroke
    box = face.getbbox(char, stroke_width=stroke)
    height = max(run.size, box[3] - min(0, box[1]))
    extra = round(height * .22) if run.italic else 0
    tile = Image.new('RGBA', (advance + extra + stroke * 2 + 3, height + stroke * 2 + 3))
    ImageDraw.Draw(tile).text((stroke, -min(0, box[1]) + stroke), char, font=face,
                             fill=run.color, stroke_width=stroke, stroke_fill=run.color)
    if run.italic:
        tile = tile.transform(tile.size, Image.Transform.AFFINE, (1, .22, -extra, 0, 1, 0), Image.Resampling.BICUBIC)
    return tile, advance, max(round(run.size * 1.4), tile.height)


def render_text(doc: TextContent):
    image = Image.new('RGB', (WIDTH, HEIGHT), doc.background)
    available = WIDTH - 2 * doc.padding
    y = doc.padding
    for paragraph in doc.paragraphs:
        lines, line, width, height = [], [], 0, 0
        pending_height = 0

        def append_line(items, w, h, wrapped=False):
            nonlocal pending_height
            pending_height += h
            if y + pending_height > HEIGHT - doc.padding:
                raise ValueError('文字超出屏幕高度，请减少内容或缩小字号')
            lines.append((items, w, h, wrapped))

        for run in paragraph.runs:
            for char in run.text:
                if char == '\r':
                    continue
                if char == '\n':
                    append_line(line, width, height or round(run.size * 1.4))
                    line, width, height = [], 0, 0
                    continue
                tile, advance, h = glyph(run, char)
                if line and width + advance + max(0, tile.width - advance) > available:
                    append_line(line, width, height, wrapped=True)
                    line, width, height = [], 0, 0
                line.append((tile, advance, char))
                width += advance
                height = max(height, h)
        default_h = round(paragraph.runs[0].size * 1.4)
        append_line(line, width, height or default_h)
        for items, w, h, wrapped in lines:
            if y + h > HEIGHT - doc.padding:
                raise ValueError('文字超出屏幕高度，请减少内容或缩小字号')
            if paragraph.align == 'justify' and wrapped:
                while items and items[-1][2].isspace():
                    w -= items.pop()[1]
            visual_w = w + (max(0, items[-1][0].width - items[-1][1]) if items else 0)
            x = doc.padding
            if paragraph.align == 'center':
                x += (available - visual_w) // 2
            elif paragraph.align == 'right':
                x += available - visual_w
            gaps = []
            if paragraph.align == 'justify' and wrapped and len(items) > 1:
                # Expand word spaces for Latin text, character gaps for CJK.
                # Last lines and explicit line breaks keep their natural width.
                gaps = [i for i, (_, _, char) in enumerate(items[:-1]) if char.isspace()]
                if not gaps:
                    gaps = list(range(len(items) - 1))
            extra = max(0, available - visual_w)
            gap_set, seen = set(gaps), 0
            for index, (tile, advance, _) in enumerate(items):
                image.paste(tile, (x, y), tile)
                x += advance
                if index in gap_set:
                    x += extra * (seen + 1) // len(gaps) - extra * seen // len(gaps)
                    seen += 1
            y += h
    used_height = y - doc.padding
    remaining = HEIGHT - 2 * doc.padding - used_height
    if doc.vertical_align != 'top' and remaining > 0:
        offset = remaining // 2 if doc.vertical_align == 'center' else remaining
        content = image.crop((0, doc.padding, WIDTH, y))
        image = Image.new('RGB', (WIDTH, HEIGHT), doc.background)
        image.paste(content, (0, doc.padding + offset))
    # Antialiasing is mapped to six colors, so preview and sent pixels match.
    return quantize(image, dither=False)

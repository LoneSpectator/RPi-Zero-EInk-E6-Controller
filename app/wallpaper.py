"""Fetch the current Bing wallpaper and detect already displayable local images."""
from dataclasses import dataclass
from datetime import datetime
import json
import time
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from PIL import Image

from .render import render_image, WIDTH, HEIGHT


BING_HOSTS = ('www.bing.com', 'cn.bing.com', 'bing.com')
METADATA_PATH = '/HPImageArchive.aspx?format=js&idx=0&n=1&mkt=zh-CN'
MAX_METADATA_BYTES = 128 * 1024
MAX_IMAGE_BYTES = 12 * 1024 * 1024


class WallpaperCancelled(Exception):
    pass


@dataclass
class Wallpaper:
    title: str
    image: Image.Image
    payload: dict


def bing_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in BING_HOSTS
            or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443)):
        raise ValueError('Bing 壁纸地址无效')
    return url


class BingRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, bing_url(newurl))


def download(opener, url, maximum, deadline, stop_event):
    if stop_event.is_set():
        raise WallpaperCancelled()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('Bing 壁纸请求超时')
    request = Request(bing_url(url), headers={'User-Agent': 'RPi-Zero-EInk-E6-Controller/1.0'})
    with opener.open(request, timeout=min(10, remaining)) as response:
        length = response.headers.get('Content-Length')
        if length and int(length) > maximum:
            raise ValueError('Bing 壁纸响应过大')
        chunks, size = [], 0
        while True:
            if stop_event.is_set():
                raise WallpaperCancelled()
            if time.monotonic() >= deadline:
                raise TimeoutError('Bing 壁纸请求超时')
            chunk = response.read(min(65536, maximum - size + 1))
            if not chunk:
                return b''.join(chunks)
            chunks.append(chunk)
            size += len(chunk)
            if size > maximum:
                raise ValueError('Bing 壁纸响应过大')


def fetch_bing_wallpaper(stop_event):
    opener = build_opener(BingRedirectHandler())
    deadline = time.monotonic() + 25
    error = None
    for host in ('www.bing.com', 'cn.bing.com'):
        try:
            base = 'https://' + host
            data = json.loads(download(opener, base + METADATA_PATH, MAX_METADATA_BYTES, deadline, stop_event).decode('utf-8-sig'))
            item = data['images'][0]
            date = datetime.strptime(item['startdate'], '%Y%m%d').date().isoformat()
            image_url = bing_url(urljoin(base, item['url']))
            image = render_image(download(opener, image_url, MAX_IMAGE_BYTES, deadline, stop_event), 'cover')
            if stop_event.is_set():
                raise WallpaperCancelled()
            return Wallpaper('Bing 今日壁纸 · ' + date, image, {
                'mode': 'cover', 'source': 'bing', 'date': date, 'url': image_url,
                'title': item.get('title', ''), 'copyright': item.get('copyright', ''),
                'copyright_link': item.get('copyrightlink', ''),
            })
        except WallpaperCancelled:
            raise
        except Exception as exc:
            error = exc
    raise ValueError(f'无法获取 Bing 今日壁纸：{error}') from error


def displayable(path):
    try:
        with Image.open(path) as image:
            if image.size != (WIDTH, HEIGHT):
                return False
            image.load()
            return True
    except (OSError, ValueError, SyntaxError, EOFError, Image.DecompressionBombError):
        return False


def has_displayable_content(store, db=None):
    if db is None:
        with store.connection() as connection:
            return has_displayable_content(store, connection)
    paths = [store.image_path(row['id']) for row in db.execute('SELECT id FROM contents')]
    state = db.execute('SELECT snapshot,source_snapshot FROM display_state').fetchone()
    paths.extend(store.root / name for name in state if name)
    paths.extend(store.root / row['source_snapshot'] for row in db.execute(
        "SELECT source_snapshot FROM jobs WHERE status IN ('pending','running') AND source_snapshot IS NOT NULL"))
    return any(displayable(path) for path in dict.fromkeys(paths))

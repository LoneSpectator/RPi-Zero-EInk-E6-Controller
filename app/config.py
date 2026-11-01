from dataclasses import dataclass, field
from pathlib import Path
import os
from zoneinfo import ZoneInfo


def env_bool(name, default):
    value = os.getenv(name, str(default)).strip().lower()
    if value not in ('true', 'false', '1', '0', 'yes', 'no', 'on', 'off'):
        raise ValueError(f'{name} must be true or false')
    return value in ('true', '1', 'yes', 'on')


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv('DATA_DIR', '/data')))
    driver: str = field(default_factory=lambda: os.getenv('DISPLAY_DRIVER', 'mock'))
    timezone: str = field(default_factory=lambda: os.getenv('TZ', 'Asia/Shanghai'))
    username: str = field(default_factory=lambda: os.getenv('WEB_USERNAME', 'admin'))
    password: str = field(default_factory=lambda: os.getenv('WEB_PASSWORD', ''))
    mock_delay: float = field(default_factory=lambda: float(os.getenv('MOCK_REFRESH_SECONDS', '2')))
    bing_default_wallpaper: bool = field(default_factory=lambda: env_bool('BING_DEFAULT_WALLPAPER', True))

    def __post_init__(self):
        self.data_dir = Path(self.data_dir)
        ZoneInfo(self.timezone)
        if self.driver not in ('mock', 'waveshare'):
            raise ValueError('DISPLAY_DRIVER must be mock or waveshare')

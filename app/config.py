from dataclasses import dataclass, field
from pathlib import Path
import os
from zoneinfo import ZoneInfo


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv('DATA_DIR', '/data')))
    driver: str = field(default_factory=lambda: os.getenv('DISPLAY_DRIVER', 'mock'))
    timezone: str = field(default_factory=lambda: os.getenv('TZ', 'Asia/Shanghai'))
    username: str = field(default_factory=lambda: os.getenv('WEB_USERNAME', 'admin'))
    password: str = field(default_factory=lambda: os.getenv('WEB_PASSWORD', ''))
    mock_delay: float = field(default_factory=lambda: float(os.getenv('MOCK_REFRESH_SECONDS', '2')))

    def __post_init__(self):
        self.data_dir = Path(self.data_dir)
        ZoneInfo(self.timezone)
        if self.driver not in ('mock', 'waveshare'):
            raise ValueError('DISPLAY_DRIVER must be mock or waveshare')
        if self.driver == 'waveshare' and not self.password:
            raise ValueError('真机模式必须设置 WEB_PASSWORD')

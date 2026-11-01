from typing import Annotated, Literal
from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict, model_validator

Color = Literal['#000000', '#ffffff', '#ff0000', '#ffff00', '#0000ff', '#00ff00']
Font = Literal['heiti', 'songti', 'kaiti', 'source-sans', 'source-serif']


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Run(StrictModel):
    text: str = Field(max_length=5000)
    color: Color = '#000000'
    font: Font = 'source-sans'
    size: int = Field(default=32, ge=12, le=120)
    bold: bool = False
    italic: bool = False


class Paragraph(StrictModel):
    align: Literal['left', 'center', 'right'] = 'left'
    runs: list[Run] = Field(min_length=1, max_length=300)


class TextContent(StrictModel):
    title: str = Field(default='', max_length=120)
    background: Color = '#ffffff'
    padding: int = Field(default=24, ge=0, le=80)
    paragraphs: list[Paragraph] = Field(min_length=1, max_length=50)

    @model_validator(mode='after')
    def check_length(self):
        if sum(len(r.text) for p in self.paragraphs for r in p.runs) > 5000:
            raise ValueError('文字总长度不能超过 5000 字')
        self.title = self.title.strip()
        if not self.title:
            for paragraph in self.paragraphs:
                for line in ''.join(run.text for run in paragraph.runs).splitlines():
                    title = ''.join(line.split())
                    if title:
                        self.title = title[:120]
                        return self
            raise ValueError('请先输入文字')
        return self


class ScheduleInput(StrictModel):
    title: str = Field(min_length=1, max_length=120)
    content_id: str
    kind: Literal['once', 'cron']
    run_at: datetime | None = None
    cron: str | None = Field(default=None, max_length=120)
    enabled: bool = True


class ContentsDelete(StrictModel):
    content_ids: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(min_length=1)


class ContentRename(StrictModel):
    title: str = Field(min_length=1, max_length=120)

    @model_validator(mode='after')
    def check_title(self):
        self.title = self.title.strip()
        if not self.title:
            raise ValueError('内容名称不能为空')
        return self

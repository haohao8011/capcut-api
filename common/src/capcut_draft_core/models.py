"""数据模型：转写片段、切点、字幕条目。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Word:
    """ASR 识别出的单个词条（带时间戳）。"""

    text: str
    start: float  # 秒
    end: float  # 秒


@dataclass
class Segment:
    """一句话/一个语义段。"""

    text: str
    start: float
    end: float
    words: list[Word] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class CutPoint:
    """视频上的一个切点（用于插入 B-roll）。"""

    time: float  # 切点位置（秒，相对主轨起点）
    reason: str = ""  # 选点原因：pause / fixed / manual


@dataclass
class Subtitle:
    """一条字幕。"""

    text: str
    start: float
    end: float

"""本地素材库扫描：递归扫指定目录，按文件大小+扩展名挑出视频文件。

kind 识别规则（用户可配）：
- 默认：扩展名是 .mp4/.mov/.mkv/.avi/.webm 都算视频
- 文件名包含 "_main" / "数字人"  → kind=main
- 文件名包含 "_broll" / "素材"  → kind=broll
- 其他按大小/用户配置猜
"""
from __future__ import annotations

import logging
import mimetypes
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)


VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".m4v", ".wmv", ".ts"}


@dataclass
class ScannedFile:
    path: str
    name: str
    size: int
    mtime: float
    kind: str = "broll"  # "main" / "broll"

    def to_asset_item(self) -> dict:
        from datetime import datetime, timezone
        return {
            "path": self.path,
            "name": self.name,
            "kind": self.kind,
            "size": self.size,
            "duration": 0.0,  # 客户端不强求探测时长，UI 上可显示"未探测"
            "mtime": datetime.fromtimestamp(self.mtime, tz=timezone.utc).isoformat()
                      if self.mtime else None,
        }


def scan_dir(root: Path, kinds: dict[str, str] | None = None) -> list[ScannedFile]:
    """递归扫 root 下的视频文件，按文件名规则打 kind 标签。"""
    if not root.exists():
        return []
    kinds = kinds or {}
    out: list[ScannedFile] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # 跳过系统/隐藏目录
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("node_modules", "__pycache__")]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in VIDEO_EXTS:
                continue
            full = os.path.join(dirpath, fn)
            try:
                st = os.stat(full)
            except OSError:
                continue
            kind = _guess_kind(fn, kinds)
            out.append(ScannedFile(
                path=os.path.abspath(full),
                name=fn,
                size=st.st_size,
                mtime=st.st_mtime,
                kind=kind,
            ))
    return out


def _guess_kind(name: str, rules: dict[str, str]) -> str:
    """根据文件名匹配用户配置的规则。"""
    name_l = name.lower()
    for pattern, kind in rules.items():
        if pattern.lower() in name_l:
            return kind
    # 默认启发式
    if "main" in name_l or "数字人" in name or "口播" in name:
        return "main"
    return "broll"


async def scan_and_upload(
    roots: list[Path],
    api,  # ServerAPI
    kinds: dict[str, str] | None = None,
    *,
    on_progress: Optional[Callable[[int, int], None]] = None,
    batch_size: int = 50,
) -> dict:
    """扫盘 + 批量上传到服务端（仅元数据）。"""
    all_files: list[ScannedFile] = []
    for root in roots:
        all_files.extend(scan_dir(root, kinds))
    log.info("扫到 %d 个视频文件", len(all_files))

    inserted = 0
    updated = 0
    for i in range(0, len(all_files), batch_size):
        batch = all_files[i:i + batch_size]
        items = [f.to_asset_item() for f in batch]
        r = api.batch_upsert_assets(items)
        if r.get("_status") == 200:
            inserted += r.get("inserted", 0)
            updated += r.get("updated", 0)
        if on_progress:
            on_progress(min(i + batch_size, len(all_files)), len(all_files))
    return {"scanned": len(all_files), "inserted": inserted, "updated": updated}

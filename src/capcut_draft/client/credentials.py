"""客户端本地凭据：把 wizard 换到的 token 存到用户目录，权限 600。

文件位置（按平台）：
- Windows: %USERPROFILE%\\.capcut-draft\\credentials.json
- Linux/macOS: ~/.capcut-draft/credentials.json

**所有素材/草稿永远不出本机；这个文件只存服务端 token + URL，不存任何素材。**
"""
from __future__ import annotations

import json
import os
import platform
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


def creds_dir() -> Path:
    """凭据目录：用户主目录下的 .capcut-draft/。"""
    if platform.system() == "Windows":
        base = Path(os.environ.get("USERPROFILE", Path.home()))
    else:
        base = Path.home()
    d = base / ".capcut-draft"
    d.mkdir(parents=True, exist_ok=True)
    return d


def creds_path() -> Path:
    return creds_dir() / "credentials.json"


@dataclass
class Credentials:
    server_url: str         # 例：http://capcut.example.com
    client_token: str       # cap_xxx
    client_id: Optional[int] = None
    client_name: Optional[str] = None
    saved_at: Optional[str] = None

    @classmethod
    def load(cls) -> Optional["Credentials"]:
        p = creds_path()
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return cls(**data)
        except Exception:
            return None

    def save(self) -> None:
        from datetime import datetime, timezone
        self.saved_at = datetime.now(timezone.utc).isoformat()
        p = creds_path()
        p.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # 权限 600（只有当前用户能读写）
        try:
            os.chmod(p, 0o600)
        except Exception:
            pass  # Windows 上 chmod 有限制

    def clear(self) -> None:
        p = creds_path()
        if p.exists():
            p.unlink()

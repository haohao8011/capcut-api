"""客户端配置。

三种加载方式（按优先级）：
1. `--config client.yaml` 显式传：读 yaml，但 server_url + client_token 仍以 credentials.json 为准
2. 隐式（不传 --config）：从 `~/.capcut-draft/credentials.json` 读 server_url + token，其它用合理默认
3. **自动发现素材目录**：扫 `D:\videos`、`D:\素材`、`/Users/$USER/Videos` 等常见路径，**零配置可用**

设计原则（用户原话）：**难活的都在服务端，员工点两下出结果**。
所以这里尽量减少"必须配的东西"：assets.dirs 没配也无所谓，缺哪个就问问要不要扫。
"""
from __future__ import annotations

import os
import platform
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import yaml  # type: ignore
    _HAVE_YAML = True
except ImportError:
    _HAVE_YAML = False

from .credentials import Credentials


# -------- 数据类 --------

@dataclass
class ServerConfig:
    url: str = "http://127.0.0.1:8000"  # 服务端地址


@dataclass
class UiConfig:
    port: int = 8001
    host: str = "127.0.0.1"  # 只绑本机，绝不暴露公网


@dataclass
class AssetsConfig:
    """本地素材库扫描配置。"""
    dirs: list[Path] = field(default_factory=list)
    kinds: dict[str, str] = field(default_factory=dict)
    rescan_interval_sec: int = 60


@dataclass
class WorkerConfig:
    poll_interval_sec: float = 3.0
    heartbeat_interval_sec: float = 20.0
    output_dir: Path = Path("./outputs")
    one_at_a_time: bool = True
    download_cache_dir: Path = Path.home() / ".capcut-draft" / "cache"
    max_cache_gb: float = 5.0


@dataclass
class ClientConfig:
    server: ServerConfig
    ui: UiConfig
    assets: AssetsConfig
    worker: WorkerConfig
    client_token: Optional[str] = None
    client_id: Optional[int] = None
    client_name: str = "未命名客户端"
    hostname: str = "unknown"
    version: str = "0.1.0"


# -------- 自动发现常见素材目录 --------

def auto_discover_assets_dirs() -> list[Path]:
    """猜一下用户机器上哪里有视频。返回存在的路径列表。"""
    home = Path.home()
    candidates: list[Path] = []
    if platform.system() == "Windows":
        # 中文 Windows 习惯路径
        for d in [
            home / "Videos",
            home / "视频",
            home / "Desktop" / "视频",
            home / "Desktop" / "数字人",
            home / "Desktop" / "素材",
            Path("D:/videos"),
            Path("D:/Videos"),
            Path("D:/数字人"),
            Path("D:/素材"),
            Path("D:/数字人口播"),
            Path("E:/videos"),
            Path("E:/素材"),
        ]:
            if d.exists() and d.is_dir():
                candidates.append(d)
    else:
        for d in [
            home / "Videos",
            home / "videos",
            home / "数字人",
            home / "素材",
            Path("/mnt/d/videos"),
            Path("/mnt/d/数字人"),
            Path("/mnt/d/素材"),
        ]:
            if d.exists() and d.is_dir():
                candidates.append(d)
    return candidates


# -------- 加载 --------

def _detect_hostname() -> str:
    if platform.system() == "Windows":
        return os.environ.get("COMPUTERNAME") or socket.gethostname()
    try:
        return os.uname().nodename
    except Exception:
        return socket.gethostname()


def load_config_from_credentials(creds: Credentials) -> ClientConfig:
    """只用 credentials.json 就能跑（不依赖任何 yaml）。"""
    return ClientConfig(
        server=ServerConfig(url=creds.server_url),
        ui=UiConfig(),
        assets=AssetsConfig(
            dirs=auto_discover_assets_dirs(),  # 自动猜
            kinds={
                "数字人": "main",
                "口播": "main",
                "主播": "main",
                "素材": "broll",
                "穿插": "broll",
            },
        ),
        worker=WorkerConfig(output_dir=Path.home() / "Videos" / "capcut-drafts"),
        client_token=creds.client_token,
        client_id=creds.client_id,
        client_name=creds.client_name or _detect_hostname(),
        hostname=_detect_hostname(),
    )


def load_config(path: str | Path) -> ClientConfig:
    """从 yaml 加载（旧路径，保留兼容）。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"客户端配置不存在: {p}\n"
            f"  复制 config/client.example.yaml 改名为 client.yaml 后再启动，"
            f"或者直接用 start-client.bat --wizard 走向导"
        )
    text = p.read_text(encoding="utf-8")
    if not _HAVE_YAML:
        return _parse_simple(text)
    data = yaml.safe_load(text) or {}

    server = ServerConfig(**(data.get("server") or {}))
    ui = UiConfig(**(data.get("ui") or {}))
    assets_raw = data.get("assets") or {}
    assets = AssetsConfig(
        dirs=[Path(x) for x in assets_raw.get("dirs", [])],
        kinds=assets_raw.get("kinds") or {},
        rescan_interval_sec=int(assets_raw.get("rescan_interval_sec", 60)),
    )
    worker_raw = data.get("worker") or {}
    worker = WorkerConfig(
        poll_interval_sec=float(worker_raw.get("poll_interval_sec", 3.0)),
        heartbeat_interval_sec=float(worker_raw.get("heartbeat_interval_sec", 20.0)),
        output_dir=Path(worker_raw.get("output_dir", "./outputs")),
        one_at_a_time=bool(worker_raw.get("one_at_a_time", True)),
    )
    cli = data.get("client") or {}
    return ClientConfig(
        server=server,
        ui=ui,
        assets=assets,
        worker=worker,
        client_token=data.get("client_token"),
        client_id=cli.get("id"),
        client_name=cli.get("name", "未命名客户端"),
        hostname=cli.get("hostname", _detect_hostname()),
        version=cli.get("version", "0.1.0"),
    )


def _parse_simple(text: str) -> ClientConfig:
    """极简 YAML 解析（无 PyYAML 时的退化路径，不支持嵌套/数组）。"""
    server_url = "http://127.0.0.1:8000"
    token = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("url:"):
            server_url = line.split(":", 1)[1].strip()
        if line.startswith("client_token:"):
            token = line.split(":", 1)[1].strip()
    return ClientConfig(
        server=ServerConfig(url=server_url),
        ui=UiConfig(),
        assets=AssetsConfig(dirs=auto_discover_assets_dirs()),
        worker=WorkerConfig(),
        client_token=token,
    )

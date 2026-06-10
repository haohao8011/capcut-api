"""客户端配置：从 YAML 读取。

示例见 `config/client.example.yaml`。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import yaml  # type: ignore
    _HAVE_YAML = True
except ImportError:
    _HAVE_YAML = False


@dataclass
class ServerConfig:
    url: str = "http://127.0.0.1:8000"  # 服务端地址
    # 注：token 在 client.yaml 单独存（敏感信息，不入 git）


@dataclass
class UiConfig:
    port: int = 8001
    host: str = "127.0.0.1"  # 只绑本机，绝不暴露公网


@dataclass
class AssetsConfig:
    """本地素材库扫描配置。"""
    dirs: list[Path] = field(default_factory=list)  # 要扫描的目录（递归）
    kinds: dict[str, str] = field(default_factory=dict)  # 文件名包含 → kind (main/broll)
    rescan_interval_sec: int = 60  # 多久重扫一次


@dataclass
class WorkerConfig:
    poll_interval_sec: float = 3.0
    heartbeat_interval_sec: float = 20.0
    output_dir: Path = Path("./outputs")  # 草稿落本地哪里
    one_at_a_time: bool = True  # 同时只跑 1 个任务（小机器友好）


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


def load_config(path: str | Path) -> ClientConfig:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"客户端配置不存在: {p}\n"
            f"  复制 config/client.example.yaml 改名为 client.yaml 后再启动"
        )
    text = p.read_text(encoding="utf-8")
    if not _HAVE_YAML:
        # 退化：手动解析 key: value（最简支持）
        return _parse_simple(text, p)
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
        hostname=cli.get("hostname", os.uname().nodename if hasattr(os, "uname") else os.environ.get("COMPUTERNAME", "unknown")),
        version=cli.get("version", "0.1.0"),
    )


def _parse_simple(text: str, p: Path) -> ClientConfig:
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
        assets=AssetsConfig(dirs=[]),
        worker=WorkerConfig(),
        client_token=token,
    )

"""启动入口：python -m capcut_draft.client

也可：python -m capcut_draft.client --port 8001 --config config/client.yaml
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from .app import run_app, set_worker  # noqa: E402
from .config import load_config  # noqa: E402
from .worker import Worker  # noqa: E402

log = logging.getLogger("capcut-client")


def main() -> None:
    ap = argparse.ArgumentParser(description="capcut-draft 客户端（本地跑 ASR + 生成草稿）")
    ap.add_argument("--config", "-c", default="config/client.yaml",
                    help="客户端配置路径（默认 config/client.yaml）")
    ap.add_argument("--port", type=int, default=None,
                    help="本地 Web UI 端口（默认读 client.yaml 里的 ui.port）")
    ap.add_argument("--no-worker", action="store_true",
                    help="只跑 Web UI，不起后台 worker（调试用）")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = load_config(args.config)
    port = args.port or cfg.ui.port
    log.info("客户端配置：server=%s, ui_port=%d, assets_dirs=%s",
             cfg.server.url, port, [str(p) for p in cfg.assets.dirs])

    if not args.no_worker:
        # 后台 worker 线程
        w = Worker(cfg)
        set_worker(w)  # 注入给 app 用
        t = threading.Thread(target=w.run_forever, name="worker", daemon=True)
        t.start()
        log.info("后台 worker 已启动")

    run_app(cfg, port=port, host=cfg.ui.host)


if __name__ == "__main__":
    main()

"""客户端启动入口。

两种使用方式：
1. **正常启动**：双击 start-client.bat → 自动读 ~/.capcut-draft/credentials.json → 起 worker + 本地 UI
2. **首次安装 / 重置**：传 --wizard → 弹窗输服务端 URL + 6 位 setup_code → 自动换 token → 起 worker

设计原则（用户原话）：**难活的都在服务端，员工点两下出结果**。
所以这里只做"调一次 redeem + 存到本地"，yaml 编辑、token 抄写都不该出现在员工视野里。
"""
from __future__ import annotations

import argparse
import getpass
import logging
import os
import platform
import socket
import sys
import threading

from .app import run_app, set_worker
from .config import ClientConfig, load_config, load_config_from_credentials
from .credentials import Credentials, creds_path
from .worker import Worker

log = logging.getLogger("capcut-client")


def _wizard() -> Credentials:
    """交互式向导：弹窗让员工输 URL + 6 位码 → 调 redeem → 存 credentials.json。"""
    print()
    print("=" * 60)
    print("  capcut-draft 客户端 · 首次配置向导")
    print("=" * 60)
    print()
    print("  这个向导只需要跑一次，之后双击 start-client.bat 即可。")
    print()
    # 1. 服务端 URL
    default_url = os.environ.get("CAPCUT_SERVER_URL", "http://")
    url = (input(f"  服务端 URL [{default_url}]: ").strip() or default_url).rstrip("/")
    if not url.startswith("http"):
        print("  ❌ URL 必须以 http/https 开头")
        sys.exit(1)
    # 2. 6 位码
    code = input("  管理员给你的 6 位安装码: ").strip().upper()
    if len(code) < 4:
        print("  ❌ 安装码太短")
        sys.exit(1)
    # 3. 客户端名（默认用本机名）
    default_name = socket.gethostname()
    name = (input(f"  这台机器的名字 [{default_name}]: ").strip() or default_name)
    # 4. hostname
    hostname = platform.node() or default_name

    print()
    print(f"  → 正在调 {url}/api/clients/wizard/redeem ...")
    from .api import ServerAPI
    api = ServerAPI(url)
    r = api.wizard_redeem(code=code, name=name, hostname=hostname)
    st = r.get("_status", 0)
    if st != 200:
        err = r.get("detail") or r.get("_error") or r.get("_text") or f"HTTP {st}"
        print(f"  ❌ 兑换失败：{err}")
        sys.exit(1)

    token = r["token"]
    creds = Credentials(
        server_url=url,
        client_token=token,
        client_id=r["client"]["id"],
        client_name=r["client"]["name"],
    )
    creds.save()
    print(f"  ✅ 兑换成功！客户端 id={creds.client_id}")
    print(f"  → token 已存到 {creds_path()}（权限 600）")
    print()
    return creds


def main() -> None:
    ap = argparse.ArgumentParser(description="capcut-draft 客户端（本地 ASR + 草稿生成）")
    ap.add_argument("--config", "-c", default=None,
                    help="可选：client.yaml 路径（不传则从 ~/.capcut-draft/credentials.json 读）")
    ap.add_argument("--wizard", action="store_true",
                    help="首次配置向导：弹窗输 URL + 6 位码，自动换 token")
    ap.add_argument("--reset", action="store_true",
                    help="清掉本地 credentials.json，下次启动会跑 wizard")
    ap.add_argument("--port", type=int, default=None,
                    help="本地 Web UI 端口（默认 8001）")
    ap.add_argument("--no-worker", action="store_true",
                    help="只跑 Web UI，不起后台 worker")
    ap.add_argument("--no-ui", action="store_true",
                    help="只跑后台 worker，不起 Web UI（适合无人值守）")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # 1. reset 模式
    if args.reset:
        creds = Credentials.load()
        if creds:
            creds.clear()
            print(f"  ✅ 已清掉 {creds_path()}")
        else:
            print("  本地没有 credentials.json，无需清")
        return

    # 2. wizard 模式
    if args.wizard:
        creds = _wizard()
    else:
        # 3. 读 credentials.json
        creds = Credentials.load()
        if not creds:
            print()
            print("  ⚠️  还没配置过客户端。")
            print("  请先找管理员拿一个 6 位安装码，然后：")
            print()
            print("     start-client.bat --wizard")
            print()
            print("  或者：先双击 install-client.bat（自动跑 wizard）")
            print()
            sys.exit(2)

    # 4. 构造配置（优先用 yaml 覆盖 URL / token，否则用 credentials）
    if args.config:
        cfg = ClientConfig.load_from_yaml(args.config)
        # 但 token / url 还是用 credentials（更安全，避免 yaml 漂移）
        cfg.server.url = creds.server_url
        cfg.client_token = creds.client_token
        if creds.client_name:
            cfg.client_name = creds.client_name
    else:
        cfg = load_config_from_credentials(creds)

    # 5. 写回 client_id（首次心跳后才知道）
    if creds.client_id:
        cfg.client_id = creds.client_id

    port = args.port or cfg.ui.port
    log.info("客户端配置：server=%s, ui_port=%d, assets_dirs=%s",
             cfg.server.url, port, [str(p) for p in cfg.assets.dirs])

    worker = None
    if not args.no_worker:
        worker = Worker(cfg)
        set_worker(worker)
        t = threading.Thread(target=worker.run_forever, name="worker", daemon=True)
        t.start()
        log.info("后台 worker 已启动")

    if not args.no_ui:
        run_app(cfg, port=port, host=cfg.ui.host)
    else:
        log.info("--no-ui 模式：worker 在前台跑（Ctrl+C 退出）")
        try:
            worker.run_forever() if worker else threading.Event().wait()
        except KeyboardInterrupt:
            log.info("Ctrl+C，退出")

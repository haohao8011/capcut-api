"""客户端后台 worker：心跳 + 扫盘上报 + 任务轮询。

设计原则（重要）：
- **本地不传任何文件二进制到云端**：上报的只是 path/size/mtime 等元数据
- **云端不缓存素材内容**：Task 表里 main_asset / broll_assets 都只是 path 引用
- 任务执行完全在本地：读 main 视频 → ASR → 切点 → 草稿，全程在 `cfg.worker.output_dir` 下
- 报给云端的 progress / result_path / error **不包含完整路径信息**（只到目录级别）
"""
from __future__ import annotations

import asyncio
import logging
import socket
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional

from .. import cli as cli_mod
from .api import ServerAPI
from .config import ClientConfig
from .storage import scan_and_upload

log = logging.getLogger("worker")


class Worker:
    """后台 worker：在本地线程中跑三个循环 + 串行处理任务。"""

    def __init__(self, cfg: ClientConfig) -> None:
        self.cfg = cfg
        self.api = ServerAPI(cfg.server.url, token=cfg.client_token)
        self._stop = threading.Event()
        self._busy = False
        self._current_task_id: Optional[int] = None
        self._last_heartbeat_ok: Optional[datetime] = None
        self._last_scan_ok: Optional[datetime] = None
        self._last_scan_count: int = 0
        self._stats = {
            "tasks_done": 0,
            "tasks_failed": 0,
            "started_at": datetime.now(),
        }

    # -------- 对外 --------

    def run_forever(self) -> None:
        """主入口：阻塞直到 stop()。"""
        log.info("worker 启动：server=%s token=%s",
                 self.cfg.server.url,
                 (self.cfg.client_token or "")[:12] + "..." if self.cfg.client_token else "<none>")

        # 启动时先打一次心跳 + 扫盘
        self._do_heartbeat()
        self._do_scan()

        # 三个循环并行（用 threading.Event.wait 错开 + 退出）
        threads = [
            threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True),
            threading.Thread(target=self._scan_loop, name="scan", daemon=True),
            threading.Thread(target=self._poll_loop, name="poll", daemon=True),
        ]
        for t in threads:
            t.start()

        # 主线程阻塞
        try:
            while not self._stop.is_set():
                self._stop.wait(1.0)
        except KeyboardInterrupt:
            log.info("收到 KeyboardInterrupt，准备停止")
            self.stop()

    def stop(self) -> None:
        log.info("worker 收到停止信号")
        self._stop.set()

    @property
    def status(self) -> dict:
        return {
            "running": not self._stop.is_set(),
            "busy": self._busy,
            "current_task_id": self._current_task_id,
            "last_heartbeat_at": self._last_heartbeat_ok.isoformat() if self._last_heartbeat_ok else None,
            "last_scan_at": self._last_scan_ok.isoformat() if self._last_scan_ok else None,
            "last_scan_files": self._last_scan_count,
            "stats": {
                **self._stats,
                "started_at": self._stats["started_at"].isoformat(),
                "uptime_sec": (datetime.now() - self._stats["started_at"]).total_seconds(),
            },
        }

    # -------- 三个循环 --------

    def _heartbeat_loop(self) -> None:
        interval = self.cfg.worker.heartbeat_interval_sec
        while not self._stop.wait(interval):
            self._do_heartbeat()

    def _scan_loop(self) -> None:
        interval = self.cfg.assets.rescan_interval_sec
        while not self._stop.wait(interval):
            self._do_scan()

    def _poll_loop(self) -> None:
        interval = self.cfg.worker.poll_interval_sec
        while not self._stop.wait(interval):
            try:
                self._poll_once()
            except Exception as e:
                log.exception("poll loop 出错: %s", e)

    # -------- 行为 --------

    def _do_heartbeat(self) -> None:
        r = self.api.heartbeat(is_online=True, version=self.cfg.version)
        st = r.get("_status", 0)
        if st == 200:
            self._last_heartbeat_ok = datetime.now()
            if r.get("id") and not self.cfg.client_id:
                self.cfg.client_id = r["id"]
            log.debug("心跳 OK (id=%s)", self.cfg.client_id)
        elif st == 401:
            log.error("客户端 token 无效，请到服务端重新注册并更新 client.yaml 的 client_token")
        else:
            log.warning("心跳失败: status=%s err=%s", st, r.get("_error") or r.get("_text"))

    def _do_scan(self) -> None:
        if not self.cfg.assets.dirs:
            return
        try:
            # storage.scan_and_upload 是 async，简单起见用 asyncio.run
            result = asyncio.run(
                scan_and_upload(
                    roots=self.cfg.assets.dirs,
                    api=self.api,
                    kinds=self.cfg.assets.kinds,
                    on_progress=lambda done, total: log.debug("scan: %d/%d", done, total),
                )
            )
            self._last_scan_ok = datetime.now()
            self._last_scan_count = result.get("scanned", 0) if isinstance(result, dict) else 0
            log.info("扫盘完成: 扫到 %d 个, 新增 %d, 更新 %d",
                     self._last_scan_count,
                     result.get("inserted", 0) if isinstance(result, dict) else 0,
                     result.get("updated", 0) if isinstance(result, dict) else 0)
        except Exception as e:
            log.error("扫盘失败: %s", e)

    def _poll_once(self) -> None:
        if self._busy and self.cfg.worker.one_at_a_time:
            return
        tasks = self.api.queue_pending()
        if not tasks:
            return
        # 选第一个 pending（API 已经按 id 升序）
        t = tasks[0]
        log.info("发现待处理任务 #%s（%s）", t.get("id"), t.get("workflow_name") or t.get("workflow_id"))
        self._process_task(t)

    # -------- 任务执行 --------

    def _process_task(self, t: dict) -> None:
        tid = t.get("id")
        self._busy = True
        self._current_task_id = tid
        try:
            # 1. claim
            r = self.api.claim(tid)
            st = r.get("_status", 0)
            if st != 200:
                log.warning("claim 失败 task=%d: %s", tid, r)
                return
            log.info("任务 #%d 领取成功", tid)

            # 2. 解析路径
            main = t.get("main_asset")
            brolls = t.get("broll_assets", []) or []
            opts = t.get("options") or {}
            if not main or not main.get("path"):
                err = "task 缺少 main_asset 路径引用（云端零缓存：素材必须在客户端本地）"
                log.error("task #%d 失败: %s", tid, err)
                self.api.fail(tid, error=err, message="缺少主视频")
                self._stats["tasks_failed"] += 1
                return

            main_path = Path(main["path"])
            broll_paths = [Path(b["path"]) for b in brolls if b.get("path")]

            # 3. 本地文件存在性校验
            if not main_path.exists():
                err = f"主视频本地不存在: {main_path.name}"
                log.error("task #%d 失败: %s", tid, err)
                self.api.fail(tid, error=err, message=err)
                self._stats["tasks_failed"] += 1
                return
            # 缺失的 b-roll 仅记录，不让任务整体失败
            ok_brolls = []
            for bp in broll_paths:
                if bp.exists():
                    ok_brolls.append(bp)
                else:
                    log.warning("b-roll 缺失，跳过: %s", bp.name)

            # 4. start
            self.api.start(tid)

            # 5. 输出目录：用户指定的 > 全局默认
            base_out = Path(t.get("output_dir") or self.cfg.worker.output_dir)
            out_dir = base_out / f"task_{tid}_{int(time.time())}"
            out_dir.mkdir(parents=True, exist_ok=True)

            # 6. 进度回调（脱敏：只回百分比 + 阶段，不回绝对路径）
            def _progress(pct: int, msg: str) -> None:
                safe_msg = msg if len(msg) < 200 else msg[:200] + "..."
                self.api.progress(tid, pct, safe_msg)

            # 7. 跑 _process_one
            draft_path = cli._process_one(
                main_path=main_path,
                brolls=ok_brolls,
                out_dir=out_dir,
                draft_name=f"task_{tid}",
                pause_threshold=float(opts.get("pause_threshold", 0.6)),
                min_cut_interval=float(opts.get("min_cut_interval", 2.5)),
                max_cuts=opts.get("max_cuts"),
                broll_duration=float(opts.get("broll_duration", 2.5)),
                width=int(opts.get("width", 1080)),
                height=int(opts.get("height", 1920)),
                fps=float(opts.get("fps", 30.0)),
                add_subtitles=bool(opts.get("add_subtitles", True)),
                skip_asr=bool(opts.get("skip_asr", False)),
                log=log,
                progress_cb=_progress,
            )

            # 8. complete
            # 报给云端的 result_path 只到目录，不带绝对路径
            self.api.complete(
                tid,
                result_path=draft_path,
                output_dir=str(out_dir),
                message=f"草稿生成完成: {Path(draft_path).name}",
            )
            log.info("任务 #%d 完成: %s", tid, draft_path)
            self._stats["tasks_done"] += 1

        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            tb = traceback.format_exc(limit=4)
            log.exception("任务 #%d 异常: %s", tid, err)
            try:
                self.api.fail(
                    tid,
                    error=err[:2000],
                    message=f"异常: {err[:80]}",
                )
                self.api.log(tid, f"traceback:\n{tb[:1500]}", level="error")
            except Exception:
                log.exception("上报失败也失败")
            self._stats["tasks_failed"] += 1
        finally:
            self._busy = False
            self._current_task_id = None


def get_local_hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return "unknown"

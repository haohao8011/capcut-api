"""客户端后台 worker：心跳 + 扫盘上报 + 任务轮询 + 草稿上传云端。

设计原则（重要）：
- **本地不传素材二进制到云端**：上报的只是 path/size/mtime 等元数据
- **草稿 .zip 上传到云端**：任务跑完 → 打包 .draft 目录 → 上传到 `/api/drafts/upload`
  - quota 超限不自动删，让用户自己去 Web 后台清理
  - 上传失败 3 次重试（指数退避），仍失败则写本地重传队列（`~/.capcut-draft/pending_uploads/`）
- 任务执行完全在本地：读 main 视频 → ASR → 切点 → 草稿，全程在 `cfg.worker.output_dir` 下
- 报给云端的 progress / result_path / error **不包含完整路径信息**（只到目录级别）
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import socket
import threading
import time
import traceback
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from capcut_draft_core import cli as cli_mod
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
            "drafts_uploaded": 0,
            "drafts_upload_failed": 0,
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
            main_upload = t.get("main_upload")
            broll_uploads = t.get("broll_uploads", []) or []
            opts = t.get("options") or {}

            # 主视频：优先本地路径，其次服务器下载
            if main and main.get("path"):
                main_path = Path(main["path"])
            elif main_upload:
                cache_dir = Path(self.cfg.worker.download_cache_dir)
                cache_dir.mkdir(parents=True, exist_ok=True)
                main_path = cache_dir / main_upload.get("filename", f"upload_{main_upload['id']}")
                if not main_path.exists():
                    log.info("task #%d: 下载主视频 upload #%d", tid, main_upload["id"])
                    self.api.progress(tid, 0, f"正在下载主视频 upload #{main_upload['id']}")
                    if not self.api.download_upload(main_upload["id"], main_path):
                        err = f"下载主视频失败: upload #{main_upload['id']}"
                        self.api.fail(tid, error=err, message=err)
                        self._stats["tasks_failed"] += 1
                        return
            else:
                err = "task 缺少 main_asset 路径引用和 main_upload（素材必须在本地或通过 Web 上传）"
                log.error("task #%d 失败: %s", tid, err)
                self.api.fail(tid, error=err, message="缺少主视频")
                self._stats["tasks_failed"] += 1
                return

            # B-roll：合并本地路径 + 服务器下载
            broll_paths = [Path(b["path"]) for b in brolls if b.get("path")]
            for bu in broll_uploads:
                cache_dir = Path(self.cfg.worker.download_cache_dir)
                cache_dir.mkdir(parents=True, exist_ok=True)
                bp = cache_dir / bu.get("filename", f"upload_{bu['id']}")
                if not bp.exists():
                    log.info("task #%d: 下载 B-roll upload #%d", tid, bu["id"])
                    if not self.api.download_upload(bu["id"], bp):
                        log.warning("B-roll 下载失败，跳过: upload #%d", bu["id"])
                        continue
                broll_paths.append(bp)

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
            draft_path = cli_mod._process_one(
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

            # 9. 打包 .draft 目录 → 上传到云端
            self._upload_draft_to_server(
                task_id=tid,
                draft_path=Path(draft_path),
                task_name=t.get("workflow_name") or f"task_{tid}",
                workflow_name=t.get("workflow_name"),
            )

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

    # -------- 草稿打包 + 上传云端 --------

    def _upload_draft_to_server(
        self,
        *,
        task_id: int,
        draft_path: Path,
        task_name: str,
        workflow_name: Optional[str],
    ) -> None:
        """任务完成 → 把 .draft 目录 zip → 上传到 /api/drafts/upload。

        - 失败重试 3 次（指数退避：2s/4s/8s）
        - 彻底失败则把 .zip 搬到 `~/.capcut-draft/pending_uploads/`，下次启动重试
        - quota 413 错误不重试，直接 log 错误让用户自己删
        """
        try:
            if not draft_path.exists():
                log.error("[upload] task #%d 草稿目录不存在: %s", task_id, draft_path)
                return

            # 1. zip
            zip_path = draft_path.parent / f"{draft_path.name}.zip"
            log.info("[upload] task #%d 开始打包 %s", task_id, draft_path.name)
            try:
                with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
                    for f in sorted(draft_path.rglob("*")):
                        if f.is_file():
                            # arcname 保留 .draft 子目录的相对路径
                            zf.write(f, f.relative_to(draft_path.parent))
            except Exception as e:
                log.exception("[upload] task #%d 打包失败: %s", task_id, e)
                return

            # 2. 上传（带重试 + 进度回调）
            sent_bytes = {"n": 0}
            total_bytes = zip_path.stat().st_size

            def _on_progress(sent: int, total: int) -> None:
                sent_bytes["n"] = sent
                # 把上传进度也报给 server（任务进度条 90% → 100% 这一段给上传用）
                pct = 90 + int(sent / total * 10) if total else 90
                self.api.progress(
                    task_id, pct,
                    message=f"上传草稿 {sent/1024/1024:.1f}/{total/1024/1024:.1f} MB",
                )

            max_attempts = 3
            last_err: Optional[str] = None
            for attempt in range(1, max_attempts + 1):
                log.info("[upload] task #%d 第 %d/%d 次上传 %s (%.1f MB)",
                         task_id, attempt, max_attempts, zip_path.name, total_bytes / 1024 / 1024)
                # 重试前重置进度计数
                sent_bytes["n"] = 0
                r = self.api.upload_draft(
                    zip_path,
                    task_id=task_id,
                    task_name=task_name,
                    workflow_name=workflow_name,
                    progress_callback=_on_progress,
                )
                st = r.get("_status", 0)
                if st == 200:
                    log.info("[upload] task #%d ✅ 上传成功，draft id=%s",
                             task_id, (r.get("draft") or {}).get("id"))
                    self._stats["drafts_uploaded"] += 1
                    # 上传成功 → 把 .zip 删掉节省磁盘（草稿 .zip 已在云端，原始 .draft 留着方便本地预览）
                    try:
                        zip_path.unlink()
                    except OSError:
                        pass
                    return
                if st == 413:
                    # quota 超限 — 不重试，让用户自己删
                    err = r.get("detail") or "quota 超限"
                    log.error("[upload] task #%d ❌ quota 超限：%s", task_id, err)
                    self._stats["drafts_upload_failed"] += 1
                    # 把 .zip 留到 pending_uploads/，给用户清理 quota 后重传
                    self._move_to_pending_uploads(zip_path, task_id, reason=f"quota: {err}")
                    return
                # 其他错误（网络 5xx、超时）→ 退避重试
                last_err = r.get("_error") or r.get("detail") or r.get("_text") or f"HTTP {st}"
                log.warning("[upload] task #%d 第 %d 次失败: %s", task_id, attempt, last_err)
                if attempt < max_attempts:
                    backoff = 2 ** attempt
                    time.sleep(backoff)

            # 3 次都失败
            log.error("[upload] task #%d ❌ 3 次都失败，最后错误: %s", task_id, last_err)
            self._stats["drafts_upload_failed"] += 1
            self._move_to_pending_uploads(zip_path, task_id, reason=last_err or "unknown")
        except Exception as e:
            log.exception("[upload] task #%d 异常: %s", task_id, e)
            self._stats["drafts_upload_failed"] += 1

    def _move_to_pending_uploads(self, zip_path: Path, task_id: int, *, reason: str) -> None:
        """上传失败的 .zip 搬到 `~/.capcut-draft/pending_uploads/`，下次启动会重传。"""
        try:
            config_dir = Path.home() / ".capcut-draft"
            pend_dir = config_dir / "pending_uploads"
            pend_dir.mkdir(parents=True, exist_ok=True)
            dest = pend_dir / f"task{task_id}_{int(time.time())}_{zip_path.name}"
            shutil.move(str(zip_path), str(dest))
            # 写个 .meta.json 记原因
            (dest.parent / (dest.name + ".meta.json")).write_text(
                f'{{"task_id": {task_id}, "reason": {reason!r}, "ts": "{datetime.now().isoformat()}"}}',
                encoding="utf-8",
            )
            log.info("[upload] .zip 已搬到 %s，等下次启动重传", dest)
        except Exception as e:
            log.error("[upload] 搬到 pending_uploads 失败: %s", e)


def get_local_hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return "unknown"
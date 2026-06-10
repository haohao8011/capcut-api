"""跟服务端 HTTP 通讯。

用 httpx 同步模式（worker 简单）。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

log = logging.getLogger(__name__)


class ServerAPI:
    def __init__(self, base_url: str, token: Optional[str] = None, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout)

    def _headers(self) -> dict:
        h = {}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _req(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.base_url}{path}"
        try:
            r = self._client.request(method, url, headers=self._headers(), **kwargs)
        except httpx.RequestError as e:
            log.error("HTTP error %s %s: %s", method, url, e)
            return {"_error": str(e), "_status": 0}
        try:
            data = r.json()
        except Exception:
            data = {"_text": r.text}
        if r.status_code >= 400:
            log.warning("HTTP %d %s %s: %s", r.status_code, method, url, data)
        return {**data, "_status": r.status_code} if isinstance(data, dict) else data

    # -------- 客户端心跳 / 任务流 --------

    def heartbeat(self, is_online: bool = True, version: str = "0.1.0") -> dict:
        return self._req("POST", "/api/clients/heartbeat",
                         json={"is_online": is_online, "version": version})

    def queue_pending(self) -> list[dict]:
        r = self._req("GET", "/api/tasks/queue/pending")
        if r.get("_status") == 200:
            return r.get("tasks", [])
        return []

    def claim(self, task_id: int) -> dict:
        return self._req("POST", f"/api/tasks/{task_id}/claim")

    def start(self, task_id: int) -> dict:
        return self._req("POST", f"/api/tasks/{task_id}/start")

    def progress(self, task_id: int, progress: int, message: str = "", level: str = "info") -> dict:
        return self._req("POST", f"/api/tasks/{task_id}/progress",
                         json={"progress": progress, "message": message, "level": level})

    def log(self, task_id: int, message: str, level: str = "info") -> dict:
        return self._req("POST", f"/api/tasks/{task_id}/log",
                         json={"level": level, "message": message})

    def complete(self, task_id: int, result_path: str,
                 output_dir: str | None = None, message: str | None = None) -> dict:
        return self._req("POST", f"/api/tasks/{task_id}/complete",
                         json={"result_path": result_path,
                               "output_dir": output_dir,
                               "message": message})

    def fail(self, task_id: int, error: str, message: str | None = None) -> dict:
        return self._req("POST", f"/api/tasks/{task_id}/fail",
                         json={"error": error, "message": message})

    # -------- 资产 --------

    def batch_upsert_assets(self, items: list[dict]) -> dict:
        return self._req("POST", "/api/assets/batch", json={"items": items})

    # -------- 健康检查 --------

    def ping(self) -> bool:
        try:
            r = self._client.get(f"{self.base_url}/login", timeout=5.0)
            return r.status_code in (200, 304)
        except Exception as e:
            log.debug("ping failed: %s", e)
            return False

    # -------- wizard：员工用 setup_code 换 token --------

    def wizard_redeem(self, code: str, name: str, hostname: str, version: str = "0.1.0") -> dict:
        """调服务端 /api/clients/wizard/redeem 换 token。返回 dict 含 token（明文）+ client 信息。"""
        return self._req("POST", "/api/clients/wizard/redeem",
                         json={"code": code, "name": name, "hostname": hostname, "version": version})

    # -------- 草稿云端存储 --------

    def upload_draft(
        self,
        zip_path: str | Path,
        *,
        task_id: Optional[int] = None,
        task_name: Optional[str] = None,
        workflow_name: Optional[str] = None,
        note: Optional[str] = None,
        chunk_size: int = 256 * 1024,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> dict:
        """把本地 .zip 草稿流式上传到服务端，命中 quota 时返回 413 错误。

        progress_callback(sent_bytes, total_bytes) — 每读完一块就调一次，UI 可以用它画进度条。
        """
        path = Path(zip_path)
        if not path.is_file():
            return {"_error": f"草稿文件不存在: {zip_path}", "_status": 0}
        total = path.stat().st_size
        if total == 0:
            return {"_error": f"草稿文件为空: {zip_path}", "_status": 0}

        def _gen():
            sent = 0
            with path.open("rb") as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    sent += len(chunk)
                    if progress_callback:
                        try:
                            progress_callback(sent, total)
                        except Exception as e:  # 进度回调出错不影响上传
                            log.debug("progress_callback 抛错（忽略）: %s", e)
                    yield chunk

        files = {"file": (path.name, _gen(), "application/zip")}
        form = {}
        if task_id is not None:
            form["task_id"] = str(task_id)
        if task_name:
            form["task_name"] = task_name
        if workflow_name:
            form["workflow_name"] = workflow_name
        if note:
            form["note"] = note
        # 上传可能要很久（2GB），单独把超时拉长
        try:
            r = self._client.post(
                f"{self.base_url}/api/drafts/upload",
                headers=self._headers(),
                files=files,
                data=form,
                timeout=httpx.Timeout(connect=15.0, read=None, write=None, pool=10.0),
            )
        except httpx.RequestError as e:
            log.error("upload_draft 网络错误: %s", e)
            return {"_error": str(e), "_status": 0}
        try:
            data = r.json()
        except Exception:
            data = {"_text": r.text}
        if r.status_code >= 400:
            log.warning("upload_draft HTTP %d: %s", r.status_code, data)
        return {**data, "_status": r.status_code} if isinstance(data, dict) else data

    def list_drafts(
        self,
        *,
        q: Optional[str] = None,
        min_size: Optional[int] = None,
        max_size: Optional[int] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        sort: str = "created_desc",
        page: int = 1,
        page_size: int = 50,
    ) -> dict:
        params: dict[str, Any] = {"sort": sort, "page": page, "page_size": page_size}
        if q: params["q"] = q
        if min_size is not None: params["min_size"] = min_size
        if max_size is not None: params["max_size"] = max_size
        if date_from: params["date_from"] = date_from
        if date_to: params["date_to"] = date_to
        return self._req("GET", "/api/drafts", params=params)

    def delete_draft(self, draft_id: int) -> dict:
        return self._req("DELETE", f"/api/drafts/{draft_id}")

    def get_draft_quota(self) -> dict:
        return self._req("GET", "/api/drafts/quota")

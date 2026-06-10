"""跟服务端 HTTP 通讯。

用 httpx 同步模式（worker 简单）。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

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

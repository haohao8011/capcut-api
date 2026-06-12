"""JSON 结构化日志（企业级：接 ELK/Loki 友好）。

用法：
- root logger 默认会带 JsonFormatter（如果 CAPCUT_LOG_JSON=1）
- 业务代码：log.info("user logged in", extra={"user_id": 42, "ip": "1.2.3.4"})
- 输出形如：{"ts":"2026-06-12T10:00:00+00:00","level":"INFO","logger":"...","msg":"...","user_id":42,"ip":"1.2.3.4"}

环境变量：
- CAPCUT_LOG_JSON=0（默认）：纯文本格式
- CAPCUT_LOG_JSON=1：JSON 格式
- CAPCUT_LOG_FILE=/path/app.log（可选）：同步写文件
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone


# LogRecord 的标准字段（这些不算"extra"）
_STD_RECORD_FIELDS = frozenset({
    "name", "msg", "args", "levelname", "levelno",
    "pathname", "filename", "module", "exc_info", "exc_text",
    "stack_info", "lineno", "funcName", "created", "msecs",
    "relativeCreated", "thread", "threadName", "processName",
    "process", "message", "asctime", "taskName",
})


class JsonFormatter(logging.Formatter):
    """把 LogRecord 序列化成 JSON 一行。

    保留标准字段（ts/level/logger/msg/exc）+ 所有 extra 字段。
    """

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
        obj: dict = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # 异常堆栈
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            obj["stack"] = self.formatStack(record.stack_info)
        # 把所有 extra 字段塞进去
        for k, v in record.__dict__.items():
            if k in _STD_RECORD_FIELDS or k.startswith("_"):
                continue
            obj[k] = v
        # JSON 序列化兜底（datetime 等）
        return json.dumps(obj, default=str, ensure_ascii=False)


def setup_json_logging() -> None:
    """给 root logger 装上 JsonFormatter（如果 CAPCUT_LOG_JSON=1）。"""
    if os.environ.get("CAPCUT_LOG_JSON", "0") != "1":
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    # 不重置其他 handler（比如 gunicorn 装的），只在最前面插一个
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    # 可选：同步写文件
    log_file = os.environ.get("CAPCUT_LOG_FILE", "").strip()
    if log_file:
        try:
            import pathlib
            pathlib.Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setFormatter(JsonFormatter())
            root.addHandler(fh)
        except OSError as e:
            logging.getLogger(__name__).warning("写日志文件失败 %s: %s", log_file, e)

"""
core/log.py — 轻量 JSON 日志模块（替代 structlog）
使用标准库 logging，输出 GCP Cloud Logging 兼容的 JSON 格式。
内存占用 < 1MB，无外部依赖。

用法（与 structlog 完全兼容）：
    from core.log import get_logger
    log = get_logger()
    log.info("event_name", key=value, ...)
    log.warning("event_name", error=str(e))
    log.error("event_name", user_id="xxx")
    log.debug("event_name")
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

from core.redaction import (
    configure_redaction_secrets,
    redact_text,
    redact_value,
)


# ── JSON Formatter ────────────────────────────────────────────────────────────

class _JsonFormatter(logging.Formatter):
    """
    把 LogRecord 格式化为单行 JSON，兼容 GCP Cloud Logging 结构：
    {"timestamp": "...", "level": "info", "event": "...", "key": "value", ...}
    """

    LEVEL_MAP = {
        logging.DEBUG:    "debug",
        logging.INFO:     "info",
        logging.WARNING:  "warning",
        logging.ERROR:    "error",
        logging.CRITICAL: "critical",
    }

    def format(self, record: logging.LogRecord) -> str:
        # 基础字段
        payload: dict[str, Any] = {
            "timestamp": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)
            ) + f".{int(record.msecs):03d}Z",
            "level":  self.LEVEL_MAP.get(record.levelno, "info"),
            "event":  redact_text(record.getMessage()),
        }

        # 附加的结构化字段（通过 extra= 传入）
        for key, val in record.__dict__.items():
            if key.startswith("_") or key in _SKIP_FIELDS:
                continue
            payload[key] = redact_value(val)

        # 异常信息
        if record.exc_info:
            payload["exc"] = redact_text(
                self.formatException(record.exc_info)
            )

        try:
            return json.dumps(payload, ensure_ascii=False, default=str)
        except Exception:
            return json.dumps({"level": "error", "event": "log_format_failed"})


_SKIP_FIELDS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname",
    "filename", "module", "exc_info", "exc_text", "stack_info",
    "lineno", "funcName", "created", "msecs", "relativeCreated",
    "thread", "threadName", "processName", "process", "message",
    "taskName",
})


# ── 全局初始化（只执行一次）──────────────────────────────────────────────────

_initialized = False

def _setup(level: str = "INFO") -> None:
    global _initialized
    if _initialized:
        return
    _initialized = True

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # 清除已有 handlers（避免重复）
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root.addHandler(handler)


# 启动时读取环境变量决定日志级别
_setup(os.environ.get("LOG_LEVEL", "INFO"))


# ── Logger 包装器（模拟 structlog 接口）──────────────────────────────────────

class _BoundLogger:
    """
    模拟 structlog 的 BoundLogger 接口：
      log.info("event", key=value)   →  {"event": "event", "key": "value", ...}
    """

    __slots__ = ("_logger",)

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)

    def _emit(self, level: int, event: str, **kw: Any) -> None:
        if not self._logger.isEnabledFor(level):
            return
        # 把 kwargs 塞进 extra，由 formatter 提取
        extra = {k: v for k, v in kw.items() if k not in _SKIP_FIELDS}
        self._logger.log(level, event, extra=extra, stacklevel=2)

    def debug(self, event: str, **kw: Any) -> None:
        self._emit(logging.DEBUG, event, **kw)

    def info(self, event: str, **kw: Any) -> None:
        self._emit(logging.INFO, event, **kw)

    def warning(self, event: str, **kw: Any) -> None:
        self._emit(logging.WARNING, event, **kw)

    # structlog 也支持 warn()
    warn = warning

    def error(self, event: str, **kw: Any) -> None:
        self._emit(logging.ERROR, event, **kw)

    def critical(self, event: str, **kw: Any) -> None:
        self._emit(logging.CRITICAL, event, **kw)

    def bind(self, **kw: Any) -> "_BoundLogger":
        """structlog 的 bind() 返回新 logger，这里简化为返回自身。"""
        return self


# ── 对外接口 ──────────────────────────────────────────────────────────────────

def get_logger(name: str = "luckagent") -> _BoundLogger:
    """替代 structlog.get_logger()，返回兼容接口的 logger。"""
    return _BoundLogger(name)


def configure(**kw: Any) -> None:
    """
    替代 structlog.configure()，接受任意参数但忽略（兼容现有调用）。
    实际配置通过 LOG_LEVEL 环境变量控制。
    """
    level = kw.get("level", os.environ.get("LOG_LEVEL", "INFO"))
    logging.getLogger().setLevel(
        getattr(logging, str(level).upper(), logging.INFO)
    )

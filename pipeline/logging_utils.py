"""Structured (JSON-lines) logging.

Each log record is one JSON object with a timestamp, level, step and message,
plus any extra key/values passed via ``extra={"ctx": {...}}``. That makes the
GitHub Actions log greppable and lets a failed run be diagnosed from the
uploaded log file alone.

Credentials are never passed to the logger; as a second line of defence the
formatter also masks the values of any SMTP_* environment variables.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_SECRET_ENV_VARS = ("SMTP_PASS", "SMTP_USER")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        ctx = getattr(record, "ctx", None)
        if ctx:
            payload.update(ctx)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        line = json.dumps(payload, default=str)
        for var in _SECRET_ENV_VARS:
            secret = os.environ.get(var)
            if secret and len(secret) >= 4:
                line = line.replace(secret, "***")
        return line


def setup_logging(log_dir: Path | None = None, level: int = logging.INFO) -> Path | None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    formatter = JsonFormatter()

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    log_file = None
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "pipeline.log"
        fh = logging.FileHandler(log_file, mode="w", encoding="utf-8")
        fh.setFormatter(formatter)
        root.addHandler(fh)

    # requests/urllib3 are chatty at INFO and could echo URLs; keep them quiet.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("matplotlib").setLevel(logging.WARNING)
    return log_file


def log(logger: logging.Logger, level: int, msg: str, **ctx) -> None:
    logger.log(level, msg, extra={"ctx": ctx})

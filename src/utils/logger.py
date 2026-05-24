import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for k, v in record.__dict__.items():
            if k in ("args", "msg", "levelname", "levelno", "pathname", "filename",
                     "module", "exc_info", "exc_text", "stack_info", "lineno",
                     "funcName", "created", "msecs", "relativeCreated", "thread",
                     "threadName", "processName", "process", "name", "taskName"):
                continue
            try:
                json.dumps(v)
                payload[k] = v
            except TypeError:
                payload[k] = repr(v)
        return json.dumps(payload, separators=(",", ":"))


def setup(level: str = "INFO", json_mode: bool = True, app_log_path: str | None = None) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # Quiet third-party libraries — httpx/httpcore log every HTTP call at INFO.
    for noisy in ("httpx", "httpcore", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(JsonFormatter() if json_mode else logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(stream)

    if app_log_path:
        Path(app_log_path).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(app_log_path)
        fh.setFormatter(JsonFormatter())
        root.addHandler(fh)


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)

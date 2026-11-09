import contextvars
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone

correlation_id_var = contextvars.ContextVar("correlation_id", default="-")

SENSITIVE = re.compile(
    r"pass|token|secret|authorization|cookie|credential|csrf|run|address|direccion|email|phone|telefono",
    re.IGNORECASE,
)


def redact(fields: dict) -> dict:
    return {k: ("***" if SENSITIVE.search(k) else v) for k, v in fields.items()}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": self.service,
            "correlationId": correlation_id_var.get(),
            "msg": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if fields:
            data.update(redact(fields))
        if record.exc_info:
            data["exception"] = record.exc_info[0].__name__
        return json.dumps(data, ensure_ascii=False, default=str)


def setup_logging(service: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    logging.getLogger("uvicorn.error").handlers = [handler]


def log(logger: logging.Logger, msg: str, **fields) -> None:
    logger.info(msg, extra={"fields": fields})

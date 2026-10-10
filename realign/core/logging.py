"""Structured logging. JSON in production, human-readable in development."""

from __future__ import annotations

import logging
import sys

import structlog

from realign.config import settings
from realign.core.redact import redacting_processor


def setup_logging() -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            # Nothing sensitive is ever rendered: tokens, passwords, e-mails,
            # card numbers, provider keys and live config secrets are masked.
            redacting_processor,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            (
                structlog.processors.JSONRenderer()
                if settings.log_json or settings.is_production
                else structlog.dev.ConsoleRenderer()
            ),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    for noisy in ("uvicorn.access", "apscheduler.executors.default", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # uvicorn writes the request line (which can carry ?token= …) through the
    # stdlib logger, so it is routed through the same redaction filter.
    class _RedactingFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            from realign.core.redact import redact

            try:
                record.msg = redact(record.getMessage())
                record.args = ()
            except Exception:  # noqa: BLE001
                record.msg = "***REDACTED***"
                record.args = ()
            return True

    for name in ("uvicorn.access", "uvicorn.error", "uvicorn"):
        target = logging.getLogger(name)
        target.addFilter(_RedactingFilter())


def get_logger(name: str = "realign") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


log = get_logger("realign")

"""Structured logging.

Spec references:
    §131 structured JSON logs; ``trace_id`` threads through HTTP, workflow,
         agent, tool, Celery, MCP and audit.
    §132 logs must never contain passwords, tokens, secrets, API keys, refresh
         tokens or hidden chain-of-thought; PII is masked by purpose.

Design notes:
    * ``structlog`` is configured to emit JSON in every non-test environment and
      a readable console renderer in tests, so pytest output stays legible.
    * Every record is passed through the redaction layer **inside the logger**.
      Application code therefore cannot leak a secret by forgetting to call a
      helper - the safe path is the default path.
    * Correlation fields come from :mod:`app.core.context`, so a log line inside
      a Celery task or an MCP handler carries the same ``trace_id`` as the HTTP
      request that caused it.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.types import EventDict, Processor

from app.core.context import RequestContext, get_context
from app.core.redaction import RedactionPurpose, redact

#: Attributes ``logging`` puts on every record that we never want in output.
_NOISY_LOG_ATTRS: frozenset[str] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


def _add_correlation(
    _logger: Any,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Attach the trusted correlation context to every record (§131)."""
    ctx: RequestContext = get_context()
    for key, value in ctx.log_fields().items():
        # Explicit values passed by the caller win, so a scoped override such as
        # `log.info(..., agent_run_id=x)` is not clobbered.
        event_dict.setdefault(key, value)
    return event_dict


def _redact_event(_logger: Any, _method_name: str, event_dict: EventDict) -> EventDict:
    """Scrub secrets and mask PII before the record is rendered (§132)."""
    redacted = redact(dict(event_dict), purpose=RedactionPurpose.TRACE)
    # `event` is the human message; it is a string, so redaction kept it a
    # string. Preserve the structlog contract of a plain string message.
    if "event" in redacted and not isinstance(redacted["event"], str):
        redacted["event"] = str(redacted["event"])
    return redacted  # type: ignore[return-value]


def _drop_noisy(_logger: Any, _method_name: str, event_dict: EventDict) -> EventDict:
    for key in list(event_dict):
        if key in _NOISY_LOG_ATTRS:
            event_dict.pop(key)
    return event_dict


def _as_extra_fields(_logger: Any, _method_name: str, event_dict: EventDict) -> EventDict:
    """Flatten ``extra={...}`` produced by the stdlib bridge."""
    extra = event_dict.pop("extra", None)
    if isinstance(extra, dict):
        for key, value in extra.items():
            event_dict.setdefault(key, value)
    return event_dict


def build_processors(*, json_output: bool) -> list[Processor]:
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        _as_extra_fields,
        _drop_noisy,
        _add_correlation,
        _redact_event,
        structlog.processors.format_exc_info,
    ]
    shared.append(
        structlog.processors.JSONRenderer(sort_keys=False, ensure_ascii=False)
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    return shared


def configure_logging(*, level: str = "INFO", json_output: bool = True) -> None:
    """Configure structlog and route stdlib logging through it.

    Safe to call more than once; the most recent call wins.
    """
    numeric_level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)

    structlog.configure(
        processors=build_processors(json_output=json_output),
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            # Records produced *by structlog itself* are already rendered.
            foreign_pre_chain=build_processors(json_output=json_output)[:-1],
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(ensure_ascii=False)
                if json_output
                else structlog.dev.ConsoleRenderer(colors=False),
            ],
        )
    )

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(numeric_level)

    # Uvicorn installs its own handlers; send its records through ours so
    # access logs carry the same trace_id and redaction guarantees.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "gunicorn.error"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers = []
        uv_logger.propagate = True

    # SQLAlchemy's echo mode prints raw statements containing parameter values;
    # that is a PII/secret hazard, so it is only ever enabled explicitly.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound logger.

    All application code uses this rather than ``logging.getLogger`` so that
    every line is structured and redacted.
    """
    return structlog.get_logger(name)  # type: ignore[no-any-return]


def configure_celery_logging() -> None:
    """Celery workers get the same pipeline, minus duplicate propagation."""
    for name in ("celery", "celery.task", "celery.worker"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True


__all__ = [
    "build_processors",
    "configure_celery_logging",
    "configure_logging",
    "get_logger",
]

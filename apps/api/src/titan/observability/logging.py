"""Structured JSON logging via structlog (TITAN-BCK-024).

Every log line is a single JSON object on stdout with the mandatory fields
``ts, level, event, request_id, trace_id, span_id, module`` (plus ``principal``
and ``project_id`` where applicable, bound by later phases). Stdlib logging
(uvicorn, alembic, sqlalchemy) is routed through the same JSON renderer so the
container emits exactly one log format.

``trace_id``/``span_id`` are emitted as ``null`` until Phase 4 wires
OpenTelemetry (TITAN-BCK-025), which replaces :func:`_add_trace_ids`.
"""

import logging
import sys
from typing import Any

import structlog
from structlog.types import EventDict, Processor


def _add_trace_ids(_: Any, __: str, event_dict: EventDict) -> EventDict:
    event_dict.setdefault("trace_id", None)
    event_dict.setdefault("span_id", None)
    return event_dict


def _ensure_request_id(_: Any, __: str, event_dict: EventDict) -> EventDict:
    event_dict.setdefault("request_id", None)
    return event_dict


_SHARED_PROCESSORS: list[Processor] = [
    structlog.contextvars.merge_contextvars,
    structlog.processors.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", key="ts"),
    structlog.processors.CallsiteParameterAdder(
        parameters=[structlog.processors.CallsiteParameter.MODULE]
    ),
    _add_trace_ids,
    _ensure_request_id,
    structlog.processors.StackInfoRenderer(),
    structlog.processors.format_exc_info,
]


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog and stdlib logging for JSON-to-stdout."""
    structlog.configure(
        processors=[
            *_SHARED_PROCESSORS,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_SHARED_PROCESSORS,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    # uvicorn's own loggers must propagate to the JSON root handler, and its
    # plaintext access log stays off — the gateway middleware emits the
    # structured http.request line instead.
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    logging.getLogger("uvicorn.access").handlers = []
    logging.getLogger("uvicorn.access").propagate = False

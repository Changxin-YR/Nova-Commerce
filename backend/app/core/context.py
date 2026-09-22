"""Request-/run-scoped context propagation.

A single :class:`contextvars.ContextVar` bag carries the correlation and
identity fields that must survive across HTTP -> service -> workflow ->
repository -> Celery -> MCP boundaries.

Spec references:
    §70  run-scoped *trusted* context. The values here originate from the
         backend only. The LLM can never write them: they are not part of
         mutable graph state (see ADR-012).
    §131 ``trace_id`` must thread through HTTP, workflow, agent, tool, Celery,
         MCP and audit.
    §89  MCP context is derived from the verified identity only.

Design notes:
    * ``contextvars`` is the correct primitive because FastAPI runs requests on
      an event loop and a Celery worker reuses processes; threadlocals would
      leak between concurrent requests.
    * Values are *immutable per scope*: the middleware binds them, and nested
      scopes use :func:`bind_context` as a context manager so nothing leaks
      back out.
"""

from __future__ import annotations

import contextlib
import secrets
import uuid
from collections.abc import Iterator
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal

ActorType = Literal["USER", "STAFF", "SYSTEM", "AGENT", "MCP", "WORKER", "ANONYMOUS"]
SourceChannel = Literal["HTTP", "AGENT", "MCP", "CELERY", "CLI", "TEST"]

TRACE_ID_HEADER = "X-Trace-Id"
REQUEST_ID_HEADER = "X-Request-Id"

#: 16 hex chars == 64 bits of correlation entropy. Wide enough that collisions
#: are not a practical concern, short enough to stay readable in logs.
_TRACE_ID_BYTES = 8

#: Sentinel timestamp for the anonymous context. Hoisted to a module constant
#: because calling ``datetime.min.replace()`` in a dataclass field default would
#: evaluate once at import time and is flagged by ruff RUF009.
_EPOCH: datetime = datetime.min.replace(tzinfo=UTC)


def new_trace_id() -> str:
    """Return a fresh correlation id."""
    return secrets.token_hex(_TRACE_ID_BYTES)


def normalise_trace_id(candidate: str | None) -> str:
    """Accept an inbound trace id only if it is safe and sane.

    An untrusted header must never be allowed to inject newlines into log
    records or grow unbounded, so the value is validated rather than echoed.
    """
    if not candidate:
        return new_trace_id()
    candidate = candidate.strip()
    if not 8 <= len(candidate) <= 64:
        return new_trace_id()
    if not all(char.isalnum() or char in "-_" for char in candidate):
        return new_trace_id()
    return candidate


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Immutable snapshot of the trusted per-request / per-run context."""

    trace_id: str
    request_id: str
    source: SourceChannel = "HTTP"

    actor_type: ActorType = "ANONYMOUS"
    actor_id: int | None = None
    user_id: int | None = None
    merchant_id: int | None = None

    roles: tuple[str, ...] = ()
    permissions: frozenset[str] = frozenset()
    #: ``SELF`` | ``MERCHANT`` | ``ALL`` - the row-level visibility scope.
    #: Resolved server-side; never accepted from a client (§104, §109 IDOR).
    data_scope: str = "SELF"

    agent_run_id: str | None = None
    agent_type: str | None = None
    client_id: str | None = None

    started_at: datetime = _EPOCH

    # -- derived helpers -------------------------------------------------
    @property
    def is_authenticated(self) -> bool:
        return self.actor_id is not None

    def has_permission(self, permission: str) -> bool:
        return permission in self.permissions

    def has_any_permission(self, *permissions: str) -> bool:
        return bool(self.permissions.intersection(permissions))

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def log_fields(self) -> dict[str, Any]:
        """Fields safe to attach to every log record.

        Deliberately excludes nothing sensitive because nothing sensitive is
        ever stored here: no tokens, no email, no phone (§132).
        """
        return {
            "trace_id": self.trace_id,
            "request_id": self.request_id,
            "source": self.source,
            "actor_type": self.actor_type,
            "actor_id": self.actor_id,
            "user_id": self.user_id,
            "merchant_id": self.merchant_id,
            "agent_run_id": self.agent_run_id,
            "agent_type": self.agent_type,
            "client_id": self.client_id,
        }

    def to_trusted_dict(self) -> dict[str, Any]:
        """The subset handed to agent graphs as run-scoped context (§70).

        Anything present here is readable by tool code and is *not* model
        controlled, because LangGraph keeps it outside the mutable state.
        """
        return {
            "trace_id": self.trace_id,
            "request_id": self.request_id,
            "user_id": self.user_id,
            "merchant_id": self.merchant_id,
            "roles": list(self.roles),
            "permissions": sorted(self.permissions),
            "data_scope": self.data_scope,
            "source": self.source,
            "agent_run_id": self.agent_run_id,
        }


_ANONYMOUS: RequestContext = RequestContext(
    trace_id="",
    request_id="",
    actor_type="ANONYMOUS",
    started_at=_EPOCH,
)
_ANONYMOUS = replace(_ANONYMOUS, trace_id="0" * 16, request_id="0" * 16)

_context: ContextVar[RequestContext] = ContextVar("nexora_request_context", default=_ANONYMOUS)


def get_context() -> RequestContext:
    """Return the active context, or a safe anonymous placeholder."""
    return _context.get()


def set_context(ctx: RequestContext) -> Token[RequestContext]:
    """Bind ``ctx`` as the active context. Prefer :func:`bind_context`."""
    return _context.set(ctx)


def reset_context(token: Token[RequestContext]) -> None:
    _context.reset(token)


@contextlib.contextmanager
def bind_context(**overrides: Any) -> Iterator[RequestContext]:
    """Temporarily overlay fields onto the active context.

    Used by workflows, Celery tasks and MCP request handlers so that anything
    logged or audited inside the block carries the right correlation ids.
    """
    current = get_context()
    allowed = set(RequestContext.__dataclass_fields__)
    unknown = set(overrides) - allowed
    if unknown:
        msg = f"unknown context field(s): {sorted(unknown)}"
        raise TypeError(msg)
    token = _context.set(replace(current, **overrides))
    try:
        yield _context.get()
    finally:
        _context.reset(token)


def new_request_context(
    *,
    trace_id: str | None = None,
    source: SourceChannel = "HTTP",
    **overrides: Any,
) -> RequestContext:
    """Build a fresh context with sensible defaults for a new scope."""
    return RequestContext(
        trace_id=normalise_trace_id(trace_id),
        request_id=uuid.uuid4().hex,
        source=source,
        started_at=datetime.now(UTC),
        **overrides,
    )


__all__ = [
    "REQUEST_ID_HEADER",
    "TRACE_ID_HEADER",
    "ActorType",
    "RequestContext",
    "SourceChannel",
    "bind_context",
    "get_context",
    "new_request_context",
    "new_trace_id",
    "normalise_trace_id",
    "reset_context",
    "set_context",
]

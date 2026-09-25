"""The MCP server: registration, filtered listing, re-authorised calling.

## The two enforcement points, and why they are where they are

Registration alone is not authorization. The SDK's list handler returns the whole
registry and its call handler runs whatever name it is given, so a server that only
registers sixteen tools *tells* every caller about all sixteen and *runs* any of
them. Spec section 90 requires ``tools/list`` to be the authorized subset and
``tools/call`` to re-authorise, and the brief adds the reason the second is not
redundant: **list-time filtering alone is never sufficient**, because a client can
cache a listing from a moment when its token held a wider grant, or simply call a
name it remembers.

That shapes this module:

* ``NovaMCPServer.list_tools`` overrides the SDK's registry query, so the filter
  runs on the exact code path a ``tools/list`` request takes. Filtering in a test
  helper or in a wrapper around ``Client`` would leave the real server listing
  everything while the tests passed.
* Every registered function is wrapped by ``_authorized_handler``, which re-decides
  authorization *inside the tool body*. This is what makes the check unbypassable:
  the SDK's own ``call_tool``, our ``call_tool``, an in-memory ``Client(server)``
  and a raw JSON-RPC POST all converge on the registered function, so
  re-authorisation happens on every path rather than on the one the author
  remembered to update.

Neither override re-implements the protocol. ``super()`` still owns schemas,
argument validation, output conversion and the JSON-RPC error mapping; this module
decides only *who may see* and *who may run*.

## Why a refusal surfaces as an ``is_error`` result

A ``ToolFailure`` raised inside the tool body becomes an ``is_error`` result whose
text begins with the numeric business code, which is the SDK's own contract for a
failed call. An authorization decision is not a transport fault: reporting it as a
JSON-RPC error would break that contract and put a Python exception type in front of
a client.
"""

from __future__ import annotations

import inspect
from typing import Any

from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations

from app.core.errors import AppError, ErrorCode
from app.mcp.auth import NovaTokenVerifier
from app.mcp.config import MCPSettings
from app.mcp.policy import TOOL_SPECS, ToolSpec, authorize_tool, authorized_tool_names
from app.mcp.tools import (
    McpErrorResult,
    McpToolError,
    _ToolOutput,
    configure_tool_runtime,
    current_principal,
    current_scopes,
    error_code_for,
)
from mcp.server import MCPServer


def _annotations(*, read_only: bool, idempotent: bool) -> ToolAnnotations:
    """Tool hints that match what the tool actually does (ADR-013, spec section 93).

    ``destructive_hint`` is ``False`` for the one write tool rather than ``True``:
    filing an approval request is additive, and a client that treated a proposal as
    destructive would prompt for a confirmation the platform does not need - which is
    how people learn to approve without reading.
    """
    return ToolAnnotations(
        read_only_hint=read_only,
        destructive_hint=None if read_only else False,
        idempotent_hint=idempotent,
        open_world_hint=False,
    )


def _tool_signature(spec: ToolSpec) -> inspect.Signature:
    """A callable signature carrying the tool's input-model fields as kwargs.

    The SDK builds ``input_schema`` by inspecting the registered function, so the
    advertised schema has to come from the *policy model* rather than from a
    hand-written parameter list. Deriving it here is what keeps one definition of the
    input shape: a tool whose model forbids ``user_id`` cannot advertise a
    ``user_id`` parameter, because there is nowhere to write one.
    """
    parameters: list[inspect.Parameter] = []
    for name, field in spec.input_model.model_fields.items():
        default = inspect.Parameter.empty if field.is_required() else field.get_default(call_default_factory=True)
        parameters.append(
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                default=default,
                annotation=field.annotation,
            )
        )
    # The declared return type is the shared output base: the SDK refuses
    # ``structured_output=True`` for an unannotated or ``Any`` return, and every
    # handler in fact returns a concrete subclass of this model. Declaring the base
    # keeps the output contract honest at the boundary without pretending all
    # sixteen tools share one shape.
    return inspect.Signature(parameters=parameters, return_annotation=_ToolOutput)


def _authorized_handler(spec: ToolSpec, handler: Any, settings: MCPSettings) -> Any:
    """Wrap one tool body with re-authorisation, then dispatch.

    The order inside the wrapper is deliberate: **authorize before parsing**. If the
    arguments were validated first, a caller could use the validator's error messages
    as an oracle for the input schema of a tool it is not authorized to see - and for
    a tool that should not exist for them at all, that is a free description of the
    surface.
    """

    async def _authorized(**kwargs: Any) -> Any:
        try:
            principal = current_principal()
            scopes = current_scopes()
        except McpToolError:
            raise McpToolError(
                ErrorCode.MCP_TOKEN_INVALID,
                "no verified bearer token is present on this request",
            ) from None

        denial = authorize_tool(
            tool_name=spec.name,
            principal=principal,
            token_scopes=scopes,
            write_scope=settings.write_scope,
        )
        if denial is not None:
            raise McpToolError(
                ErrorCode[denial.code],
                f"{spec.name}: {denial.reason}",
            )

        # Re-validated here rather than trusting the SDK's parse: this body is the
        # common path for every transport, and parsing a small model twice is cheaper
        # than the two paths ever disagreeing about what was authorised.
        try:
            parsed = spec.input_model.model_validate(kwargs)
        except Exception as exc:
            raise McpToolError(
                ErrorCode.AGENT_TOOL_INPUT_INVALID,
                f"invalid arguments for {spec.name}: {exc}",
            ) from exc

        # Every tool body runs through here, so this is the one place that has to know
        # how the platform's refusals become MCP refusals. A service raises ``AppError``
        # with a stable business code (50003 "order not found", 20010 "data scope"), and a
        # plain ``AppError`` would be classified by the SDK as a *crash*: the client would
        # receive "Error executing tool ..." with the code withheld, which is exactly the
        # information an agent needs in order to decide whether to retry.
        try:
            result = await handler(principal, parsed)
        except McpToolError:
            raise
        except AppError as exc:
            raise McpToolError(error_code_for(exc), f"{spec.name}: {exc.public_message}") from exc

        # A tool that refused reports it as a value. The SDK's call path only accepts an
        # output model, so the refusal is converted once, here at the boundary - which is
        # also the only place that knows the tool's name to put in the message.
        if isinstance(result, McpErrorResult):
            raise McpToolError(result.code, f"{spec.name}: {result.message}") from None
        return result

    _authorized.__name__ = spec.name.replace(".", "_")
    _authorized.__qualname__ = _authorized.__name__
    _authorized.__signature__ = _tool_signature(spec)  # type: ignore[attr-defined]
    return _authorized


class NovaMCPServer(MCPServer[Any]):
    """``MCPServer`` whose listing is authorization-filtered and whose calls re-authorise."""

    def __init__(self, settings: MCPSettings, *, token_verifier: NovaTokenVerifier) -> None:
        super().__init__(
            name=settings.server_name,
            title=settings.server_title,
            description=(
                "Nova Commerce operations surface. Read-only commerce tools plus one "
                "approval-gated promotion proposal."
            ),
            version=settings.server_version,
            token_verifier=token_verifier,
            auth=AuthSettings(
                # Both are validated as absolute URLs by app.core.config.Settings; the SDK wants
                # its own URL type, hence the two narrow, local ignores.
                issuer_url=settings.issuer_url,  # type: ignore[arg-type]
                resource_server_url=settings.resource_server_url,  # type: ignore[arg-type]
                required_scopes=list(settings.required_scopes),
                # True is what makes the bearer middleware compare the token's RFC
                # 8707 resource indicator against this server, so a token minted for
                # the REST API cannot be replayed here even with a valid signature.
                validate_token_resource=True,
            ),
            # A duplicate tool name is a programming error, not something to warn
            # about and move past: two tools with one name means one of them is
            # unreachable, and a warning is how that ships.
            warn_on_duplicate_tools=False,
        )
        self._nova_settings = settings

    # -- listing -----------------------------------------------------------
    async def list_tools(self) -> list[Any]:
        """The surface filtered by token scope intersected with RBAC, DataScope and policy.

        With no verified token the answer is the empty list rather than the full
        registry: a missing principal means authorization could not be evaluated, and
        the only safe result of "cannot decide" is "nothing is available". Returning
        everything would make the *unauthenticated* listing the most permissive one
        this server can produce.
        """
        tools = await super().list_tools()
        try:
            principal = current_principal()
            scopes = current_scopes()
        except McpToolError:
            return []
        allowed = set(
            authorized_tool_names(
                principal=principal,
                token_scopes=scopes,
                write_scope=self._nova_settings.write_scope,
            )
        )
        return [tool for tool in tools if tool.name in allowed]

    # -- calling -----------------------------------------------------------
    async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> Any:
        """Refuse an unexposed name, otherwise delegate to the re-authorising body.

        The registry check happens before the SDK's lookup so that a capability that
        must not exist (``execute_sql``, ``refund.execute``) is refused with
        ``MCP_TOOL_NOT_EXPOSED`` - the same outcome as "unknown tool", but stating the
        rule being enforced rather than reporting a lookup miss. It also means a name
        somebody accidentally registers later still cannot be called.
        """
        if not any(spec.name == name for spec in TOOL_SPECS):
            # Raised, not returned: the SDK's call path only accepts a result model,
            # and an authorization refusal is an anticipated failure - which is what
            # the SDK's own ToolError path is for. Returning a bare object here would
            # surface as a 500 "Internal server error" and tell the caller nothing.
            raise McpToolError(ErrorCode.MCP_TOOL_NOT_EXPOSED, f"{name}: tool is not exposed by this server")
        return await super().call_tool(name, arguments, context)


def build_mcp_server(settings: MCPSettings | None = None) -> NovaMCPServer:
    """Build the server and register all sixteen tools.

    Registration is data-driven from ``app.mcp.policy.TOOL_SPECS``, and a spec with no
    handler is a hard failure at build time. That is the point: a tool that is listed
    but cannot run is worse than a missing tool, because a client plans around a
    capability that then fails at the moment it matters.
    """
    resolved = settings or MCPSettings.load()
    configure_tool_runtime(proposal_ttl_seconds=resolved.proposal_ttl_seconds)
    verifier = NovaTokenVerifier(resolved)
    server = NovaMCPServer(resolved, token_verifier=verifier)

    # Imported here rather than at module scope: ``tools`` imports the service layer,
    # so a process that only wants the policy surface does not pull in SQLAlchemy
    # models just to read the tool registry.
    from app.mcp import tools as tool_module

    for spec in TOOL_SPECS:
        handler = tool_module.HANDLERS.get(spec.name)
        if handler is None:  # pragma: no cover - asserted by the registry test
            msg = f"no handler is registered for {spec.name!r}"
            raise RuntimeError(msg)
        server.tool(
            name=spec.name,
            title=spec.title,
            description=spec.description,
            annotations=_annotations(read_only=spec.read_only, idempotent=spec.idempotent),
            structured_output=True,
        )(_authorized_handler(spec, handler, resolved))

    return server


def transport_security(settings: MCPSettings) -> TransportSecuritySettings:
    """The spec-section-88 transport hardening, taken from configuration."""
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=settings.enable_dns_rebinding_protection,
        allowed_hosts=list(settings.allowed_hosts),
        allowed_origins=list(settings.allowed_origins),
    )


__all__ = ["NovaMCPServer", "build_mcp_server", "transport_security"]

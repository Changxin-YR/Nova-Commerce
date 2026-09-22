# ADR-013 — MCP SDK Compatibility Baseline

- **Status:** Accepted
- **Date:** 2026-09-22
- **Spec references:** §12, §86–§93, §121, §138 (ADR-013), §151
- **Supersedes:** none

---

## Context

Spec §12 freezes the MCP Design Baseline at protocol revision **`2026-07-28`**
and simultaneously imposes four constraints that interact:

1. The MCP wire protocol **must not be hand-written**.
2. The **current official MCP SDK** must be used.
3. Before real MCP development, the SDK version, supported protocol revisions,
   Streamable HTTP lifecycle, stdio behaviour, auth behaviour and
   legacy-client strategy must be **recorded**.
4. If the stable SDK supports **both** 2026 and 2025-era clients, its **native
   compatibility must be used**, and official compatibility must not be
   artificially broken.

Spec §151 additionally warns: *"不要复制过期 MCP Tutorial"* — do not copy stale
MCP tutorials. §12 makes any protocol baseline change require an ADR **plus** a
compatibility test.

## Decision

**Use `mcp==2.2.0` (official Python SDK) directly, with its native protocol
negotiation, and do not implement any version handling ourselves.**

## Evidence

Established by introspection of the installed package, not by reading prose
(it is not possible to satisfy constraint 1 by consulting documentation alone —
the API surface must be observed):

```
mcp.__version__                    == 2.2.0
mcp.types.LATEST_PROTOCOL_VERSION  == '2026-07-28'      # == spec §12 baseline
mcp.types.DEFAULT_NEGOTIATED_VERSION == '2025-03-26'    # legacy floor
mcp.types.JSONRPC_VERSION          == '2.0'
mcp.types.UNSUPPORTED_PROTOCOL_VERSION == -32022
mcp.types.PROTOCOL_VERSION_META_KEY == 'io.modelcontextprotocol/protocolVersion'
```

Therefore the spec's frozen `2026-07-28` baseline is **not aspirational** — it is
the SDK's current latest revision, and legacy 2025-era clients are negotiated
natively. Constraint 4 is satisfied without any custom code.

### Verified capability surface (mapped to spec requirements)

| Spec requirement | Verified API |
|---|---|
| §87 OAuth Resource Server | `AuthSettings(issuer_url, resource_server_url, validate_token_resource, required_scopes, client_registration_options, revocation_options)`; `AccessToken(token, client_id, scopes, expires_at, resource, subject, claims)`; `TokenVerifier.verify_token()`; middleware chain `AuthenticationMiddleware(BearerAuthBackend)` + `AuthContextMiddleware` + `RequireAuthMiddleware` |
| §88 transport security | `mcp.server.transport_security.TransportSecuritySettings(enable_dns_rebinding_protection, allowed_hosts, allowed_origins)`; `TransportSecurityMiddleware`; `RequestBodyLimitMiddleware`; `DEFAULT_MAX_REQUEST_BODY_SIZE` |
| §86 transports | `MCPServer.streamable_http_app()`; `mcp.run(transport=..., session_idle_timeout=..., max_sessions=...)`; stdio present |
| §93 tool schema & read/write semantics | `Tool(name, description, input_schema, output_schema, annotations, meta)`; `ToolAnnotations(read_only_hint, destructive_hint, idempotent_hint, open_world_hint)`; `CallToolResult(structured_content, content, is_error)` |

## Breaking API delta that MUST be respected (v1 → v2)

```
v1:  from mcp.server.fastmcp import FastMCP      ;  mcp = FastMCP("Demo")
v2:  from mcp.server import MCPServer            ;  mcp = MCPServer("Demo")
```

`import mcp.server.fastmcp` raises `ModuleNotFoundError` on 2.2.0. **Verified
by attempting the import.** Every MCP tutorial written for the v1 line will fail
immediately on this dependency set; the implementation therefore targets
`MCPServer` exclusively, and Phase 0 recorded this as gap **G-04**.

Client-side note, also verified: there is **no `headers=` kwarg**; auth headers,
proxies and timeouts are supplied via a configured `httpx.AsyncClient` passed to
`streamable_http_client(url, http_client=...)`.

## Consequences

**Positive**

- Protocol versions, negotiation and error codes (`-32022`) come from the SDK;
  the project carries no bespoke wire code, as §12 demands.
- The 2026 baseline and the 2025-era floor are both covered natively, so §12's
  "prefer native compatibility" clause is honoured literally.
- OAuth Resource Server semantics and transport security are configuration, not
  invention — this materially de-risks FG-18.

**Negative / accepted**

- The dependency is on a v2-major SDK whose API differs from the overwhelming
  majority of published examples. Mitigation: this ADR is the project's single
  source of truth for the MCP API surface, and `tests/mcp/test_sdk_compatibility.py`
  asserts the invariants above against the *installed* package, so a future SDK
  bump fails loudly in CI rather than silently changing behaviour.
- A future renaming/removal inside the SDK will break the import. The
  compatibility test is the tripwire.

## Compliance with §12's change-control rule

`LATEST_PROTOCOL_VERSION` is pinned by the dependency pin `mcp==2.2.0`. Any
change to that pin that moves the protocol baseline **requires**:

1. a new/extended ADR, and
2. the compatibility test `tests/mcp/test_sdk_compatibility.py` (FG-18) passing
   against the new version, including legacy-client negotiation.

This ADR is the record required by §12's "must record" clause: SDK version
(`2.2.0`), supported protocol versions (`2026-07-28` latest, `2025-03-26`
default-negotiated floor), Streamable HTTP lifecycle (`streamable_http_app()`
with `session_idle_timeout` / `max_sessions`), stdio behaviour (supported),
auth behaviour (OAuth Resource Server via `TokenVerifier`/`AuthSettings`), and
the legacy-client strategy (**native negotiation — nothing custom, nothing
broken**).

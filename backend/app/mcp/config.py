"""MCP configuration: a *projection* of the canonical application settings.

## There is exactly one environment contract in this repository

:class:`app.core.config.Settings` owns every key the deployment reads, including the
``MCP_*`` keys this resource server uses. This module does not parse the environment at
all: it is a typed view that re-exposes those values under the lowercase attribute names
``app/mcp/**`` already reads (``resource_server_url``, ``max_request_body_size``, ...),
so the resource server keeps its short local names without a second model re-declaring
the same variables.

That duplication was the earlier arrangement and it drifted in exactly the way the
settings-contract test exists to catch: ``.env.example`` documented
``MCP_MAX_REQUEST_BODY_SIZE`` while the model that "owned" body size read
``MCP_MAX_REQUEST_BODY_BYTES``, which is a setting an operator can write and nothing
reads. Two models parsing one namespace will always drift; one model plus a projection
cannot.

The projection is built by :meth:`MCPSettings.from_core`, and the mapping is written out
field by field on purpose - a rename on either side then fails loudly at that one place
instead of silently shadowing a key.

## Why the type is a model and not a bare dataclass

``tests/unit/core/test_settings_contract.py`` reads ``MCPSettings.model_fields`` when it
builds the union of declared keys (so that a future ``MCP_*`` field on *this* type would
still have to be documented). Keeping a real field set here means that check keeps
working, and every field below is populated from :class:`Settings` rather than from the
environment.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, PrivateAttr, SecretStr

from app.core.config import DEFAULT_MCP_ISSUER_URL, DEFAULT_MCP_RESOURCE_SERVER_URL, Settings

#: Re-exported for callers that only need the defaults. The values live on
#: :class:`Settings`, so these aliases cannot drift from the model that validates them.
DEFAULT_RESOURCE_SERVER_URL = DEFAULT_MCP_RESOURCE_SERVER_URL
DEFAULT_ISSUER_URL = DEFAULT_MCP_ISSUER_URL


def _any_configured(*values: str) -> bool:
    """True when at least one of ``values`` is a non-empty string.

    Extracted so the "is anything configured" question has one implementation and one
    place to be typed: the alternative - a boolean expression inline in a property - is
    where a stray falsy default silently turns an unconfigured server into a configured
    one. Empty strings, not ``None``, arrive here because both secret fields are
    ``SecretStr`` whose unset value is ``""``.
    """
    return any(value.strip() for value in values)


class MCPSettings(BaseModel):
    """The resource server's view of the canonical settings.

    Field names are the lowercase names ``app/mcp/**`` reads; each one is assigned from
    a ``Settings`` field by :meth:`from_core`. Nothing here is read from the environment,
    so a key that exists only in this class cannot silently do nothing - it would simply
    have no value to be assigned from.
    """

    #: ``extra="forbid"`` so a name that is not part of the projection is a construction
    #: error rather than a silently dropped keyword - the same discipline as the tool
    #: input models, for the same reason: an ignored key looks like configuration.
    #:
    #: Deliberately **not** ``frozen``. A pydantic private attribute cannot be passed to
    #: the constructor (not being a field is what makes it private), so ``from_core``
    #: assigns the one alias after construction; a frozen model would refuse that
    #: assignment. Immutability would be pleasant but nothing here depends on it: the
    #: projection is built by one factory and never mutated afterwards, and the only value
    #: that must not drift - :attr:`has_key_material` - is derived on every access instead
    #: of stored.
    model_config = ConfigDict(extra="forbid")

    # -- identity of the resource server ----------------------------------
    server_name: str
    server_title: str
    server_version: str
    enabled: bool
    http_path: str
    resource_server_url: str
    issuer_url: str

    # -- key material ------------------------------------------------------
    hmac_secret: SecretStr | None
    public_key_pem: SecretStr | None
    jwks_url: str | None
    algorithm: str
    leeway_seconds: int

    # -- authorization -----------------------------------------------------
    required_scopes: list[str]
    write_scope: str

    # -- transport security ------------------------------------------------
    allowed_hosts: list[str]
    allowed_origins: list[str]
    enable_dns_rebinding_protection: bool
    session_idle_timeout_seconds: float
    max_sessions: int
    request_timeout_seconds: int

    # -- proposals ---------------------------------------------------------
    proposal_ttl_seconds: int

    #: Single private carrier for the body-size alias. Private attributes are not model
    #: fields, which is what keeps the alias out of the environment contract; the
    #: annotation is declared so the type checker can see the attribute at all.
    _body_size: int = PrivateAttr(default=0)

    @classmethod
    def from_core(cls, core: Settings) -> MCPSettings:
        """Project the canonical settings onto the resource server's vocabulary.

        Every keyword below is the explicit rename between the two namespaces, which is
        the whole content of this module. A field added to :class:`Settings` and read by
        the resource server gets one line here; a field that is *not* listed is simply not
        part of this server's view.
        """
        view = cls(
            server_name=core.MCP_SERVER_NAME,
            server_title=core.MCP_SERVER_TITLE,
            server_version=core.MCP_SERVER_VERSION,
            enabled=core.MCP_ENABLED,
            http_path=core.MCP_HTTP_PATH,
            resource_server_url=core.MCP_RESOURCE_SERVER_URL,
            issuer_url=core.MCP_ISSUER_URL,
            # ``None`` rather than an empty ``SecretStr``: the verifier asks "is a key
            # configured", and an empty secret that is truthy would make an unconfigured
            # server look configured.
            hmac_secret=core.MCP_HMAC_SECRET if core.MCP_HMAC_SECRET.get_secret_value() else None,
            public_key_pem=(
                core.MCP_PUBLIC_KEY_PEM if core.MCP_PUBLIC_KEY_PEM.get_secret_value() else None
            ),
            jwks_url=core.MCP_JWKS_URL or None,
            algorithm=core.MCP_ALGORITHM,
            leeway_seconds=core.MCP_LEEWAY_SECONDS,
            required_scopes=list(core.MCP_REQUIRED_SCOPES),
            write_scope=core.MCP_WRITE_SCOPE,
            allowed_hosts=list(core.MCP_ALLOWED_HOSTS),
            allowed_origins=list(core.MCP_ALLOWED_ORIGINS),
            enable_dns_rebinding_protection=core.MCP_ENABLE_DNS_REBINDING_PROTECTION,
            session_idle_timeout_seconds=float(core.MCP_SESSION_IDLE_TIMEOUT_SECONDS),
            max_sessions=core.MCP_MAX_SESSIONS,
            request_timeout_seconds=core.MCP_REQUEST_TIMEOUT_SECONDS,
            proposal_ttl_seconds=core.MCP_PROPOSAL_TTL_SECONDS,
        )
        # A pydantic private attribute cannot be passed to the constructor (not being a
        # field is the point), so the one alias is assigned here. Doing it in the factory
        # rather than exposing a settable field is what keeps the alias out of the model's
        # field set - and therefore out of the environment contract.
        view._body_size = core.MCP_MAX_REQUEST_BODY_BYTES
        return view

    @property
    def max_request_body_size(self) -> int:
        """The canonical ``MCP_MAX_REQUEST_BODY_BYTES`` under this server's local name.

        A read-only property rather than a field. A field would appear in
        ``model_fields``, and the settings-contract test turns every field name here into
        an environment key (``MCP_MAX_REQUEST_BODY_SIZE``) that no model parses - which is
        exactly the drift this task exists to remove. A property is not a field, so the
        alias cannot invent a second variable, and it cannot be set to disagree with the
        key it maps either.

        The mapping is written out in :meth:`from_core`; there is no ``getattr``-based
        fallback anywhere in this module, so a rename on either side fails loudly there.
        """
        return self._body_size

    @property
    def has_key_material(self) -> bool:
        """Whether any verification key is configured. Derived on every access.

        Derived rather than stored because it *is* the answer the verifier trusts when it
        decides to refuse a token: a stored copy could say ``True`` while all three keys
        were blank, and "looks configured, refuses everything" is the failure this flag is
        meant to make impossible. The question is asked with the same three fields
        :meth:`app.core.config.Settings.mcp_has_key_material` asks about, so the startup
        guard and the request path cannot disagree.
        """
        return _any_configured(
            self.hmac_secret.get_secret_value() if self.hmac_secret else "",
            self.public_key_pem.get_secret_value() if self.public_key_pem else "",
            self.jwks_url or "",
        )

    @classmethod
    def load(cls, **overrides: object) -> MCPSettings:
        """Project the process-wide settings, or an explicit override of them.

        With no arguments this is the projection of ``get_settings()`` - the production
        path, and the only constructor the resource server uses. ``**overrides`` are
        passed as keyword arguments to :class:`app.core.config.Settings`, which
        ``pydantic-settings`` prioritises over the environment, so a test can build a
        second, differently configured server without touching ``os.environ`` or the
        cached process settings. The HTTP suite depends on that: it runs a deliberately
        unscoped server next to the configured one in a single process.

        The import is inside the method so this module performs no environment read at
        import time - the settings cache is consulted when a server is built, not when the
        module is loaded.
        """
        from app.core.config import Settings, get_settings

        core = get_settings() if not overrides else Settings(**overrides)  # type: ignore[arg-type]
        return cls.from_core(core)

    @property
    def required_scopes_with_write(self) -> list[str]:
        """The scopes ``AuthSettings`` advertises, i.e. everything any tool may need.

        The per-tool intersection is enforced by :mod:`app.mcp.policy`; this list is only
        what the bearer middleware requires of *any* request, so it must not include a
        scope no tool needs or every token would be refused for the wrong reason.
        """
        return [*self.required_scopes, self.write_scope]


__all__ = [
    "DEFAULT_ISSUER_URL",
    "DEFAULT_RESOURCE_SERVER_URL",
    "MCPSettings",
]

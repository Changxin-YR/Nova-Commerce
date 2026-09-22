"""The settings <-> .env.example contract.

Spec §134 requires a committed ``.env.example``; spec §150 requires facts over
intentions. Together they mean the template must be *provably* complete, so this
test fails the build when the two drift apart in either direction:

  * a settings field with no documented key   -> operators cannot configure it
  * a documented key with no settings field   -> operators set it and nothing happens

The second direction is the more dangerous one, because a silently ignored
setting looks configured.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core.config import ENV_FILES, Settings, get_settings

# parents: [0]=core [1]=unit [2]=tests [3]=backend [4]=<repo root>
REPO_ROOT = Path(__file__).resolve().parents[4]
ENV_EXAMPLE = REPO_ROOT / ".env.example"

_KEY_LINE = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=")


def _documented_keys() -> set[str]:
    keys: set[str] = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8-sig").splitlines():
        match = _KEY_LINE.match(line)
        if match:
            keys.add(match.group(1))
    return keys


@pytest.fixture(scope="module")
def documented() -> set[str]:
    assert ENV_EXAMPLE.is_file(), f"{ENV_EXAMPLE} must exist (spec §134)"
    return _documented_keys()


def test_env_example_is_not_empty(documented: set[str]) -> None:
    assert len(documented) > 80, "the template should document the full configuration surface"


def test_every_settings_field_is_documented(documented: set[str]) -> None:
    """No field may be configurable-but-undocumented."""
    undocumented = sorted(set(Settings.model_fields) - documented)
    assert not undocumented, (
        "these settings exist but are missing from .env.example, so an operator "
        f"cannot discover them: {undocumented}"
    )


def test_every_documented_key_is_a_settings_field(documented: set[str]) -> None:
    """No key may be documented-but-ignored."""
    unknown = sorted(documented - set(Settings.model_fields))
    assert not unknown, (
        "these keys are documented in .env.example but no setting reads them, so "
        f"setting them would silently do nothing: {unknown}"
    )


def test_env_file_paths_are_anchored_to_the_source_tree() -> None:
    """Regression guard for a real Phase 1 bug.

    ``env_file=(".env",)`` resolves against the process working directory, so
    launching uvicorn from ``backend/`` missed a root-level ``.env`` entirely and
    fell back to defaults - including an *empty* database password. The failure
    appeared as "MySQL is not reachable", which is a misleading symptom.
    """
    for path in ENV_FILES:
        assert path.is_absolute(), f"{path} must be absolute, not CWD-relative"
    assert ENV_FILES[0].parent == REPO_ROOT
    assert ENV_FILES[1].parent == REPO_ROOT / "backend"


def test_settings_load_independently_of_cwd(tmp_path, monkeypatch) -> None:
    """Loading must not depend on where the process was started."""
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    try:
        resolved = get_settings()
        assert resolved.MYSQL_PORT > 0
        assert resolved.DATABASE_URL.startswith("mysql+pymysql://")
    finally:
        get_settings.cache_clear()


def test_derived_urls_are_assembled_from_components() -> None:
    """A missing DATABASE_URL must be derived, not left blank."""
    resolved = Settings(
        MYSQL_HOST="db.internal",
        MYSQL_PORT=3307,
        MYSQL_DATABASE="shop",
        MYSQL_USER="svc",
        MYSQL_PASSWORD="pw",
        DATABASE_URL="",
    )
    assert resolved.DATABASE_URL == "mysql+pymysql://svc:pw@db.internal:3307/shop?charset=utf8mb4"


def test_explicit_database_url_wins_over_components() -> None:
    resolved = Settings(DATABASE_URL="mysql+pymysql://x:y@h:1/z?charset=utf8mb4")
    assert resolved.DATABASE_URL.endswith("/z?charset=utf8mb4")


@pytest.mark.parametrize("env", ["prod", "staging"])
def test_production_rejects_placeholder_jwt_secret(env: str) -> None:
    """§23 + §134: a placeholder secret in production must fail loudly."""
    with pytest.raises(ValueError, match="JWT_SECRET_KEY is empty or a placeholder"):
        Settings(
            APP_ENV=env,  # type: ignore[arg-type]
            APP_DEBUG=False,
            JWT_SECRET_KEY="REPLACE_ME_WITH_64_BYTES_OF_ENTROPY",
            REFRESH_COOKIE_SECURE=True,
            AI_USE_FAKE_PROVIDERS=False,
        )


def test_production_requires_secure_refresh_cookie() -> None:
    """§23: Secure must be set in production, so the check must bite."""
    with pytest.raises(ValueError, match="REFRESH_COOKIE_SECURE must be true"):
        Settings(
            APP_ENV="prod",
            APP_DEBUG=False,
            JWT_SECRET_KEY="a" * 64,
            REFRESH_COOKIE_SECURE=False,
            AI_USE_FAKE_PROVIDERS=False,
        )


def test_production_rejects_debug_and_fake_providers() -> None:
    with pytest.raises(ValueError) as excinfo:
        Settings(
            APP_ENV="prod",
            APP_DEBUG=True,
            JWT_SECRET_KEY="a" * 64,
            REFRESH_COOKIE_SECURE=True,
            AI_USE_FAKE_PROVIDERS=True,
        )
    message = str(excinfo.value)
    assert "APP_DEBUG must be false" in message
    assert "AI_USE_FAKE_PROVIDERS must be false" in message


def test_production_cannot_disable_dns_rebinding_protection() -> None:
    """§88: the MCP transport guard must not be switchable off in production."""
    with pytest.raises(ValueError, match="MCP_ENABLE_DNS_REBINDING_PROTECTION"):
        Settings(
            APP_ENV="prod",
            APP_DEBUG=False,
            JWT_SECRET_KEY="a" * 64,
            REFRESH_COOKIE_SECURE=True,
            AI_USE_FAKE_PROVIDERS=False,
            MCP_ENABLE_DNS_REBINDING_PROTECTION=False,
        )


def test_valid_production_configuration_is_accepted() -> None:
    resolved = Settings(
        APP_ENV="prod",
        APP_DEBUG=False,
        JWT_SECRET_KEY="a" * 64,
        REFRESH_COOKIE_SECURE=True,
        AI_USE_FAKE_PROVIDERS=False,
    )
    assert resolved.is_production
    assert not resolved.is_testing


def test_chunk_overlap_must_be_smaller_than_chunk_size() -> None:
    """§54: a nonsensical chunking config should fail at startup, not mid-ingest."""
    with pytest.raises(ValueError, match="RAG_CHUNK_OVERLAP_TOKENS must be smaller"):
        Settings(RAG_CHUNK_MAX_TOKENS=128, RAG_CHUNK_OVERLAP_TOKENS=128)


def test_csv_settings_accept_both_forms() -> None:
    """Comma-separated values must work, since that is what .env syntax allows."""
    from_comma = Settings(CORS_ALLOW_ORIGINS="http://a.test,http://b.test")
    assert from_comma.CORS_ALLOW_ORIGINS == ["http://a.test", "http://b.test"]

    from_empty = Settings(CORS_ALLOW_ORIGINS="")
    assert from_empty.CORS_ALLOW_ORIGINS == []


def test_log_level_is_normalised_and_validated() -> None:
    assert Settings(LOG_LEVEL="debug").LOG_LEVEL == "DEBUG"
    with pytest.raises(ValueError, match="LOG_LEVEL must be one of"):
        Settings(LOG_LEVEL="verbose")


def test_secrets_are_not_exposed_by_repr() -> None:
    """A leaked secret in a traceback or log line is a real incident path."""
    resolved = Settings(JWT_SECRET_KEY="super-secret-value", S3_SECRET_KEY="another-secret")
    assert "super-secret-value" not in repr(resolved)
    assert "another-secret" not in repr(resolved)
    assert resolved.JWT_SECRET_KEY.get_secret_value() == "super-secret-value"


def test_all_buckets_are_distinct() -> None:
    """§20: knowledge must be private and separate from product images."""
    resolved = Settings()
    buckets = resolved.all_buckets
    assert len(set(buckets)) == len(buckets), "bucket names must not collide"
    assert resolved.S3_BUCKET_KNOWLEDGE != resolved.S3_BUCKET_PRODUCT_IMAGES

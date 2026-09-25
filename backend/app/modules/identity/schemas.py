"""Identity request/response schemas and the wire projection of identity rows.

## Why the list endpoints are the ones that need this file

A router is the transport edge: it validates, resolves the principal, calls one
service method and wraps the result in the section-95 envelope. The request bodies
and the two projections below are therefore *interface* types, not domain types -
which is why they live in the module rather than inline in ``api/``. Keeping them
here means the console, the agent tool gateway and the MCP server can all shape the
same payload without importing a router (``api/`` is not an import target).

``profile_data`` and ``address_data`` are plain functions rather than pydantic
models because both are *derived* views: ``profile_data`` masks PII with the
redaction layer (section 94) and ``address_data`` deliberately omits
``merchant_id``/``user_id``/timestamps, which an owner-scoped endpoint has no
reason to publish.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.redaction import mask_email, mask_phone
from app.modules.identity.enums import AddressTag
from app.modules.identity.models import User, UserAddress


class LoginBody(BaseModel):
    """``POST /auth/login``."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=1024)


class AddressCreate(BaseModel):
    """``POST /auth/users/addresses``."""

    model_config = ConfigDict(extra="forbid")

    receiver_name: str = Field(min_length=1, max_length=64)
    receiver_phone: str = Field(min_length=6, max_length=32)
    province: str = Field(max_length=64)
    city: str = Field(max_length=64)
    district: str = Field(max_length=64)
    detail: str = Field(min_length=1, max_length=255)
    postal_code: str | None = Field(default=None, max_length=16)
    tag: AddressTag = AddressTag.HOME
    is_default: bool = False


class AddressUpdate(BaseModel):
    """``PUT /auth/users/addresses/{address_id}``.

    Every field is optional so a caller can patch one column, but an explicitly
    supplied ``null`` is refused for anything except ``postal_code``: ``None`` here
    means "leave it alone", never "clear it", and the two are distinguishable only
    by ``model_fields_set``.
    """

    model_config = ConfigDict(extra="forbid")

    receiver_name: str | None = Field(default=None, min_length=1, max_length=64)
    receiver_phone: str | None = Field(default=None, min_length=6, max_length=32)
    province: str | None = Field(default=None, max_length=64)
    city: str | None = Field(default=None, max_length=64)
    district: str | None = Field(default=None, max_length=64)
    detail: str | None = Field(default=None, min_length=1, max_length=255)
    postal_code: str | None = Field(default=None, max_length=16)
    tag: AddressTag | None = None
    is_default: bool | None = None

    @model_validator(mode="after")
    def required_columns_cannot_be_null(self) -> AddressUpdate:
        for name in self.model_fields_set - {"postal_code"}:
            if getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


def profile_data(user: User) -> dict:
    """The account view returned by ``/auth/users/me`` (and by login/refresh).

    Contact details are masked here rather than at the edge: a projection that
    leaks on one path is a leak, and there are three paths that return this shape.
    """
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "email": mask_email(user.email) if user.email else None,
        "phone": mask_phone(user.phone) if user.phone else None,
        "roles": list(user.role_codes),
        "merchant_id": user.merchant_id,
    }


def address_data(row: UserAddress) -> dict:
    """The owner-visible view of one address row."""
    return {
        "id": row.id,
        "receiver_name": row.receiver_name,
        "receiver_phone": row.receiver_phone,
        "province": row.province,
        "city": row.city,
        "district": row.district,
        "detail": row.detail,
        "postal_code": row.postal_code,
        "tag": row.tag,
        "is_default": row.is_default,
    }


__all__ = [
    "AddressCreate",
    "AddressUpdate",
    "LoginBody",
    "address_data",
    "profile_data",
]

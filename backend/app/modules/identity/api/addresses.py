"""Owner-scoped address book endpoints used by checkout."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.core.errors import envelope
from app.modules.identity.dependencies import CurrentPrincipal
from app.modules.identity.enums import AddressTag
from app.modules.identity.models import UserAddress
from app.modules.identity.service import AddressService
from app.shared.db.session import get_session

router = APIRouter()
SessionDep = Annotated[Session, Depends(get_session)]


class AddressCreate(BaseModel):
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


def address_data(row: UserAddress) -> dict:
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


@router.get("/users/addresses", summary="List my addresses")
def list_addresses(principal: CurrentPrincipal, session: SessionDep) -> dict:
    items = [address_data(row) for row in AddressService(session).list(user_id=principal.user_id)]
    return envelope(data={
        "items": items,
        "meta": {"page": 1, "page_size": 20, "total": len(items), "total_pages": 1 if items else 0},
    })


@router.post("/users/addresses", summary="Create my address")
def create_address(body: AddressCreate, principal: CurrentPrincipal, session: SessionDep) -> dict:
    row = AddressService(session).create(
        user_id=principal.user_id,
        merchant_id=principal.merchant_id,
        **body.model_dump(),
    )
    data = address_data(row)
    session.commit()
    return envelope(data=data)


@router.put("/users/addresses/{address_id}", summary="Update my address")
def update_address(address_id: int, body: AddressUpdate, principal: CurrentPrincipal, session: SessionDep) -> dict:
    row = AddressService(session).update(
        user_id=principal.user_id,
        address_id=address_id,
        changes=body.model_dump(exclude_unset=True),
    )
    data = address_data(row)
    session.commit()
    return envelope(data=data)


@router.delete("/users/addresses/{address_id}", summary="Remove my address")
def remove_address(address_id: int, principal: CurrentPrincipal, session: SessionDep) -> dict:
    AddressService(session).delete(user_id=principal.user_id, address_id=address_id)
    session.commit()
    return envelope(data=None)


@router.post("/users/addresses/{address_id}/default", summary="Choose my default address")
def set_default(address_id: int, principal: CurrentPrincipal, session: SessionDep) -> dict:
    row = AddressService(session).set_default(user_id=principal.user_id, address_id=address_id)
    data = address_data(row)
    session.commit()
    return envelope(data=data)

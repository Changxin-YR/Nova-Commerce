"""Owner-scoped address book endpoints used by checkout.

Transport only: parse, principal, one ``AddressService`` call, envelope. The
address book's transaction belongs to the service (section 49), so no handler here
touches the session or commits it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.errors import envelope
from app.modules.identity.dependencies import CurrentPrincipal
from app.modules.identity.schemas import AddressCreate, AddressUpdate, address_data
from app.modules.identity.service import AddressService
from app.shared.db.session import get_session

router = APIRouter()
SessionDep = Annotated[Session, Depends(get_session)]


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
    return envelope(data=address_data(row))


@router.put("/users/addresses/{address_id}", summary="Update my address")
def update_address(address_id: int, body: AddressUpdate, principal: CurrentPrincipal, session: SessionDep) -> dict:
    row = AddressService(session).update(
        user_id=principal.user_id,
        address_id=address_id,
        changes=body.model_dump(exclude_unset=True),
    )
    return envelope(data=address_data(row))


@router.delete("/users/addresses/{address_id}", summary="Remove my address")
def remove_address(address_id: int, principal: CurrentPrincipal, session: SessionDep) -> dict:
    AddressService(session).delete(user_id=principal.user_id, address_id=address_id)
    return envelope(data=None)


@router.post("/users/addresses/{address_id}/default", summary="Choose my default address")
def set_default(address_id: int, principal: CurrentPrincipal, session: SessionDep) -> dict:
    row = AddressService(session).set_default(user_id=principal.user_id, address_id=address_id)
    return envelope(data=address_data(row))

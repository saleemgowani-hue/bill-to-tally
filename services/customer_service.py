"""Customer resolution for the Sales pipeline — mirrors find_or_create_supplier."""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from database.models import Customer


def find_or_create_customer(
    session: Session, company_id: int, name: str, gstin: str = "", address: str = ""
) -> Optional[Customer]:
    name = (name or "").strip()
    if not name:
        return None
    query = session.query(Customer).filter(Customer.company_id == company_id)
    if gstin:
        found = query.filter(Customer.gstin == gstin).first()
        if found:
            return found
    found = query.filter(Customer.name.ilike(name)).first()
    if found:
        if gstin and not found.gstin:
            found.gstin = gstin
        return found
    customer = Customer(
        company_id=company_id,
        name=name[:200],
        gstin=gstin or None,
        address=address or None,
        state_code=(gstin or "")[:2] or None,
    )
    session.add(customer)
    session.flush()
    return customer

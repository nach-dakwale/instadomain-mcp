"""Pydantic request/response models for the InstaDomain API."""
from __future__ import annotations

import re

from pydantic import BaseModel, field_validator


class BulkCheckRequest(BaseModel):
    domains: list[str]

    @field_validator("domains")
    @classmethod
    def max_fifty(cls, v: list[str]) -> list[str]:
        if len(v) > 50:
            raise ValueError("Maximum 50 domains per request")
        return v


class RegistrantContact(BaseModel):
    first_name: str
    last_name: str
    email: str
    org_name: str = ""
    address1: str
    city: str
    state: str
    postal_code: str
    country: str  # 2-letter ISO code
    phone: str  # format: +1.5551234567

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        if not re.match(r"^\+\d{1,3}\.\d{4,14}$", v):
            raise ValueError(
                "Phone must be in EPP format: +CC.NNNNnnnnnn "
                "(e.g. +1.5551234567). Country code 1-3 digits, "
                "then a dot, then 4-14 digits."
            )
        return v

    @field_validator("country")
    @classmethod
    def validate_country(cls, v: str) -> str:
        if not re.match(r"^[A-Z]{2}$", v):
            raise ValueError(
                "Country must be a 2-letter uppercase ISO code (e.g. US, CA, GB)"
            )
        return v


class BuyRequest(BaseModel):
    domain: str
    registrant: RegistrantContact

"""OpenSRS XML API client for domain registration."""
from __future__ import annotations

import secrets
import string

import httpx

from instadomain.opensrs_xml import (
    OpenSRSError,  # noqa: F401 - re-exported for existing imports
    build_envelope,
    parse_response,
    sign,
)

# Default WHOIS privacy contact
DEFAULT_CONTACT = {
    "first_name": "InstaDomain",
    "last_name": "Privacy",
    "org_name": "InstaDomain Privacy Service",
    "address1": "PO Box 0000",
    "city": "San Francisco",
    "state": "CA",
    "postal_code": "94102",
    "country": "US",
    # TODO: Replace with a real InstaDomain business phone number
    "phone": "+1.4153300000",
    "email": "privacy@instadomain.dev",
}


class OpenSRSClient:
    """XML API client for OpenSRS domain registration."""

    def __init__(
        self,
        api_key: str,
        reseller_username: str,
        api_url: str = "https://rr-n1-tor.opensrs.net:55443",
    ):
        self.api_key = api_key
        self.reseller_username = reseller_username
        self.api_url = api_url

    @staticmethod
    def _random_password(length: int = 16) -> str:
        """Generate a random registrant password for OpenSRS."""
        alphabet = string.ascii_letters + string.digits + "!@#$"
        return "".join(secrets.choice(alphabet) for _ in range(length))

    def _post(self, xml_body: str) -> httpx.Response:
        """Sign and send an XML request to the OpenSRS API."""
        signature = sign(xml_body, self.api_key)
        headers = {
            "Content-Type": "text/xml",
            "X-Username": self.reseller_username,
            "X-Signature": signature,
        }
        return httpx.post(self.api_url, content=xml_body, headers=headers)

    def check_availability(self, domain: str) -> bool:
        """Check if a domain is available for registration via OpenSRS LOOKUP.

        Returns True if the domain is available, False otherwise.
        """
        attrs = {"domain": domain}
        xml_body = build_envelope("LOOKUP", "DOMAIN", attrs)
        response = self._post(xml_body)
        data = parse_response(response.text)
        attrs_data = data.get("attributes", {})
        return attrs_data.get("status") == "available"

    def get_price(self, domain: str) -> int:
        """Look up the wholesale registration price for a domain.

        Returns the price in cents.
        """
        attrs = {"domain": domain, "period": 1}
        xml_body = build_envelope("GET_PRICE", "DOMAIN", attrs)
        response = self._post(xml_body)
        data = parse_response(response.text)

        attrs_data = data.get("attributes", {})
        price_str = attrs_data.get("price", "0")
        return int(round(float(price_str) * 100))

    def register(
        self,
        domain: str,
        years: int,
        registrant_email: str,
        nameservers: list[str],
        registrant_contact: dict | None = None,
    ) -> dict:
        """Register a domain with WHOIS privacy.

        When registrant_contact is provided, the user is set as owner
        (legal registrant) and billing contact. InstaDomain stays as
        admin and tech contact for service management. Falls back to
        DEFAULT_CONTACT when no registrant_contact is given.

        Returns dict with order_id and expiry.
        """
        if registrant_contact:
            owner_contact = {**registrant_contact}
            billing_contact = {**registrant_contact}
            admin_contact = {**DEFAULT_CONTACT, "email": registrant_email}
            tech_contact = {**DEFAULT_CONTACT, "email": registrant_email}
        else:
            owner_contact = {**DEFAULT_CONTACT, "email": registrant_email}
            admin_contact = owner_contact
            billing_contact = owner_contact
            tech_contact = owner_contact

        contact_set = {
            "owner": owner_contact,
            "admin": admin_contact,
            "billing": billing_contact,
            "tech": tech_contact,
        }
        ns_list = [
            {"name": ns, "sortorder": str(i + 1)}
            for i, ns in enumerate(nameservers)
        ]

        attrs = {
            "domain": domain,
            "period": str(years),
            "reg_username": self.reseller_username,
            "reg_password": self._random_password(),
            "reg_type": "new",
            "handle": "process",
            "custom_nameservers": "1",
            "custom_tech_contact": "1",
            "f_lock_domain": "1",
            "f_whois_privacy": "1",
            "auto_renew": "0",
            "nameserver_list": ns_list,
            "contact_set": contact_set,
        }

        xml_body = build_envelope("SW_REGISTER", "DOMAIN", attrs)
        response = self._post(xml_body)
        data = parse_response(response.text)

        attrs_data = data.get("attributes", {})
        return {
            "order_id": attrs_data.get("id", ""),
            "expiry": attrs_data.get("registration_expiration_date", ""),
        }

    def renew_domain(self, domain: str, current_expiry_year: int, years: int = 1) -> dict:
        """Renew a domain for additional years.

        current_expiry_year is required by OpenSRS to prevent duplicate
        renewals. Pass the year the domain currently expires (e.g. 2027).

        Returns dict with order_id, new expiry date, and admin email.
        """
        attrs = {
            "domain": domain,
            "period": str(years),
            "handle": "process",
            "auto_renew": "0",
            "currentexpirationyear": str(current_expiry_year),
        }

        xml_body = build_envelope("RENEW", "DOMAIN", attrs)
        response = self._post(xml_body)
        data = parse_response(response.text)

        attrs_data = data.get("attributes", {})
        return {
            "order_id": attrs_data.get("id", ""),
            "expiry": attrs_data.get("registration_expiration_date", ""),
            "admin_email": attrs_data.get("admin_email", ""),
        }

    def get_transfer_auth_code(self, domain: str) -> str:
        """Request the EPP/transfer authorization code for a domain."""
        attrs = {"domain_name": domain}
        xml_body = build_envelope("SEND_AUTHCODE", "DOMAIN", attrs)
        response = self._post(xml_body)
        data = parse_response(response.text)
        attrs_data = data.get("attributes", {})
        return attrs_data.get("domain_auth_info", "")

    def unlock_domain(self, domain: str) -> None:
        """Remove the registrar transfer lock from a domain."""
        attrs = {"domain": domain, "data": "status", "lock_state": "0"}
        xml_body = build_envelope("MODIFY", "DOMAIN", attrs)
        response = self._post(xml_body)
        parse_response(response.text)

    def lock_domain(self, domain: str) -> None:
        """Apply the registrar transfer lock to a domain."""
        attrs = {"domain": domain, "data": "status", "lock_state": "1"}
        xml_body = build_envelope("MODIFY", "DOMAIN", attrs)
        response = self._post(xml_body)
        parse_response(response.text)

    def update_nameservers(self, domain: str, nameservers: list[str]) -> None:
        """Update the nameservers for a domain."""
        ns_list = list(nameservers)
        attrs = {
            "domain": domain,
            "op_type": "assign",
            "assign_ns": ns_list,
        }
        xml_body = build_envelope("ADVANCED_UPDATE_NAMESERVERS", "DOMAIN", attrs)
        response = self._post(xml_body)
        parse_response(response.text)

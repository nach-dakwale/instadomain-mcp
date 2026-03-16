from __future__ import annotations

import hashlib
import secrets
import string
import xml.etree.ElementTree as ET

import httpx


class OpenSRSError(Exception):
    """Error returned by the OpenSRS API."""

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"OpenSRS error {code}: {message}")


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
    "phone": "+1.0000000000",
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

    def _sign(self, xml_body: str) -> str:
        """Compute MD5 signature: md5(md5(xml + key) + key)."""
        inner = hashlib.md5((xml_body + self.api_key).encode()).hexdigest()
        return hashlib.md5((inner + self.api_key).encode()).hexdigest()

    def _dict_to_xml(self, d: dict | list) -> str:
        """Convert a dict or list to OpenSRS XML format (dt_assoc/dt_array)."""
        if isinstance(d, list):
            items = []
            for i, v in enumerate(d):
                if isinstance(v, (dict, list)):
                    items.append(f'<item key="{i}">{self._dict_to_xml(v)}</item>')
                else:
                    items.append(f'<item key="{i}">{v}</item>')
            return f"<dt_array>{' '.join(items)}</dt_array>"

        items = []
        for key, value in d.items():
            if isinstance(value, (dict, list)):
                items.append(f'<item key="{key}">{self._dict_to_xml(value)}</item>')
            else:
                items.append(f'<item key="{key}">{value}</item>')
        return f"<dt_assoc>{' '.join(items)}</dt_assoc>"

    def _build_envelope(self, action: str, obj: str, attrs: dict) -> str:
        """Build an OpenSRS XML request envelope."""
        attrs_xml = self._dict_to_xml(attrs)
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="no"?>'
            "<!DOCTYPE OPS_envelope SYSTEM \"ops.dtd\">"
            "<OPS_envelope>"
            "<header><version>0.9</version></header>"
            "<body>"
            "<data_block>"
            "<dt_assoc>"
            f'<item key="protocol">XCP</item>'
            f'<item key="action">{action}</item>'
            f'<item key="object">{obj}</item>'
            f'<item key="attributes">{attrs_xml}</item>'
            "</dt_assoc>"
            "</data_block>"
            "</body>"
            "</OPS_envelope>"
        )

    def _parse_assoc(self, elem: ET.Element) -> dict:
        """Parse a dt_assoc element into a dict."""
        result = {}
        for item in elem.findall("item"):
            key = item.get("key")
            child_assoc = item.find("dt_assoc")
            child_array = item.find("dt_array")
            if child_assoc is not None:
                result[key] = self._parse_assoc(child_assoc)
            elif child_array is not None:
                result[key] = self._parse_array(child_array)
            else:
                result[key] = item.text or ""
        return result

    def _parse_array(self, elem: ET.Element) -> list:
        """Parse a dt_array element into a list."""
        items = []
        for item in elem.findall("item"):
            child_assoc = item.find("dt_assoc")
            child_array = item.find("dt_array")
            if child_assoc is not None:
                items.append(self._parse_assoc(child_assoc))
            elif child_array is not None:
                items.append(self._parse_array(child_array))
            else:
                items.append(item.text or "")
        return items

    def _parse_response(self, xml_text: str) -> dict:
        """Parse an OpenSRS XML response into a dict.

        Raises OpenSRSError if the response indicates failure.
        """
        root = ET.fromstring(xml_text)
        body_assoc = root.find(".//body/data_block/dt_assoc")
        if body_assoc is None:
            raise OpenSRSError(0, "Malformed response: no data_block dt_assoc found")

        data = self._parse_assoc(body_assoc)

        if data.get("is_success") != "1":
            code = int(data.get("response_code", 0))
            message = data.get("response_text", "Unknown error")
            raise OpenSRSError(code, message)

        return data

    def _post(self, xml_body: str) -> httpx.Response:
        """Sign and send an XML request to the OpenSRS API."""
        signature = self._sign(xml_body)
        headers = {
            "Content-Type": "text/xml",
            "X-Username": self.reseller_username,
            "X-Signature": signature,
        }
        return httpx.post(self.api_url, content=xml_body, headers=headers)

    def get_price(self, domain: str) -> int:
        """Look up the wholesale registration price for a domain.

        Returns the price in cents.
        """
        attrs = {"domain": domain, "period": 1}
        xml_body = self._build_envelope("GET_PRICE", "DOMAIN", attrs)
        response = self._post(xml_body)
        data = self._parse_response(response.text)

        # OpenSRS returns price as a string like "8.90" (dollars)
        attrs_data = data.get("attributes", {})
        price_str = attrs_data.get("price", "0")
        return int(round(float(price_str) * 100))

    def register(
        self,
        domain: str,
        years: int,
        registrant_email: str,
        nameservers: list[str],
    ) -> dict:
        """Register a domain with WHOIS privacy and default contact info.

        Returns dict with order_id and expiry.
        """
        contact = {**DEFAULT_CONTACT, "email": registrant_email}
        contact_set = {
            "owner": contact,
            "admin": contact,
            "billing": contact,
            "tech": contact,
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
            "custom_tech_contact": "0",
            "f_lock_domain": "1",
            "f_whois_privacy": "1",
            "auto_renew": "0",
            "nameserver_list": ns_list,
            "contact_set": contact_set,
        }

        xml_body = self._build_envelope("SW_REGISTER", "DOMAIN", attrs)
        response = self._post(xml_body)
        data = self._parse_response(response.text)

        attrs_data = data.get("attributes", {})
        return {
            "order_id": attrs_data.get("id", ""),
            "expiry": attrs_data.get("registration_expiration_date", ""),
        }

    def update_nameservers(self, domain: str, nameservers: list[str]) -> None:
        """Update the nameservers for a domain."""
        ns_list = {str(i + 1): {"name": ns} for i, ns in enumerate(nameservers)}
        attrs = {
            "domain": domain,
            "op_type": "assign",
            "assign_ns": ns_list,
        }

        xml_body = self._build_envelope("ADVANCED_UPDATE_NAMESERVERS", "DOMAIN", attrs)
        response = self._post(xml_body)
        self._parse_response(response.text)

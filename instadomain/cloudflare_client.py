from __future__ import annotations

import httpx


class CloudflareError(Exception):
    """Error returned by the Cloudflare API."""

    pass


class CloudflareClient:
    """Async client for the Cloudflare API."""

    BASE_URL = "https://api.cloudflare.com/client/v4"

    def __init__(self, api_token: str, account_id: str):
        self.account_id = account_id
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={"Authorization": f"Bearer {api_token}"},
        )

    def _raise_on_error(self, data: dict) -> None:
        """Raise CloudflareError if the API response indicates failure."""
        if not data.get("success", False):
            errors = data.get("errors", [])
            if errors:
                msg = "; ".join(e.get("message", str(e)) for e in errors)
            else:
                msg = "Unknown Cloudflare API error"
            raise CloudflareError(msg)

    async def get_zone_by_name(self, domain: str) -> dict | None:
        """Look up an existing zone by domain name.

        Returns dict with zone_id and nameservers if found, None otherwise.
        """
        resp = await self._client.get("/zones", params={"name": domain})
        data = resp.json()
        self._raise_on_error(data)

        results = data.get("result", [])
        if not results:
            return None

        zone = results[0]
        return {
            "zone_id": zone["id"],
            "nameservers": zone["name_servers"],
        }

    async def create_zone(self, domain: str) -> dict:
        """Create a DNS zone for a domain.

        Returns dict with zone_id and nameservers.
        """
        resp = await self._client.post(
            "/zones",
            json={
                "name": domain,
                "account": {"id": self.account_id},
                "type": "full",
            },
        )
        data = resp.json()
        self._raise_on_error(data)

        result = data["result"]
        return {
            "zone_id": result["id"],
            "nameservers": result["name_servers"],
        }

    async def create_dns_token(self, zone_id: str, domain: str) -> str:
        """Create a scoped API token for DNS management of a specific zone.

        Returns the token value string.

        Note: Permission group IDs below are placeholders.
        """
        DNS_WRITE_PERMISSION_GROUP = "4755a26eedb94da69e1066d98aa820be"
        DNS_READ_PERMISSION_GROUP = "82e64a83756745bbbb1c9c2701bf816b"

        resp = await self._client.post(
            "/user/tokens",
            json={
                "name": f"InstaDomain DNS - {domain}",
                "policies": [
                    {
                        "effect": "allow",
                        "resources": {
                            f"com.cloudflare.api.account.zone.{zone_id}": "*",
                        },
                        "permission_groups": [
                            {"id": DNS_WRITE_PERMISSION_GROUP},
                            {"id": DNS_READ_PERMISSION_GROUP},
                        ],
                    }
                ],
            },
        )
        data = resp.json()
        self._raise_on_error(data)

        return data["result"]["value"]

    async def delete_zone(self, zone_id: str) -> None:
        """Delete a DNS zone."""
        resp = await self._client.delete(f"/zones/{zone_id}")
        data = resp.json()
        self._raise_on_error(data)

    async def close(self) -> None:
        """Close the underlying httpx client."""
        await self._client.aclose()

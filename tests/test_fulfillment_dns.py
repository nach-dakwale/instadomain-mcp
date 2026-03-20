import asyncio

from instadomain import fulfillment as fulfillment_module


def test_fulfillment_marks_dns_pending_when_zone_creation_fails(monkeypatch):
    order_store = {
        "id": "ord_dns",
        "domain": "example",
        "tld": "com",
        "status": "registering",
        "email": "buyer@example.com",
        "registration_years": 1,
        "stripe_payment_intent": "pi_123",
        "amount_cents": 1999,
        "retry_count": 0,
        "cloudflare_zone_id": None,
        "nameservers": None,
        "registrant_contact": {
            "first_name": "Test",
            "last_name": "User",
            "email": "buyer@example.com",
            "org_name": "",
            "address1": "123 Test St",
            "city": "San Francisco",
            "state": "CA",
            "postal_code": "94102",
            "country": "US",
            "phone": "+1.4155551234",
        },
    }
    status_updates = []
    scheduled_retries = []
    refunds = []

    async def fake_get_order(_pool, _order_id):
        return dict(order_store)

    async def fake_update_order_status(_pool, _order_id, new_status, **fields):
        order_store["status"] = new_status
        order_store.update(fields)
        status_updates.append((new_status, fields))
        return dict(order_store)

    async def fake_increment_retry_count(_pool, _order_id):
        order_store["retry_count"] += 1
        return order_store["retry_count"]

    def fake_schedule_dns_retry(**kwargs):
        scheduled_retries.append(kwargs["retry_count"])

    async def fake_send_purchase_success_email(**_kwargs):
        raise AssertionError("success email should not be sent on dns_pending")

    monkeypatch.setattr(fulfillment_module, "get_order", fake_get_order)
    monkeypatch.setattr(fulfillment_module, "update_order_status", fake_update_order_status)
    monkeypatch.setattr(fulfillment_module, "_increment_retry_count", fake_increment_retry_count)
    monkeypatch.setattr(fulfillment_module, "_schedule_dns_retry", fake_schedule_dns_retry)
    monkeypatch.setattr(fulfillment_module, "issue_refund", lambda payment_intent: refunds.append(payment_intent))
    monkeypatch.setattr(fulfillment_module, "send_purchase_success_email", fake_send_purchase_success_email)

    class OpenSRS:
        def register(self, **_kwargs):
            return {"order_id": "osrs_1", "expiry": "2027-03-19T00:00:00"}

        def update_nameservers(self, *_args, **_kwargs):
            return None

    class FailingCloudflare:
        async def create_zone(self, _domain):
            raise RuntimeError("cf unavailable")

    result = asyncio.run(
        fulfillment_module.fulfill_order(
            pool=object(),
            order_id="ord_dns",
            opensrs=OpenSRS(),
            cloudflare=FailingCloudflare(),
            encryption_key="key",
        )
    )

    assert result["status"] == "dns_pending"
    assert "Cloudflare zone creation failed" in result["error_msg"]
    assert refunds == []
    assert scheduled_retries == [1]
    assert [status for status, _fields in status_updates] == ["setting_dns", "dns_pending"]

from __future__ import annotations

import asyncio
import logging
import os
import smtplib
from email.message import EmailMessage

logger = logging.getLogger(__name__)


def _smtp_config() -> dict[str, str | int] | None:
    host = os.environ.get("SMTP_HOST")
    from_addr = os.environ.get("SMTP_FROM")
    if not host or not from_addr:
        logger.info("SMTP not configured; skipping email delivery")
        return None

    return {
        "host": host,
        "port": int(os.environ.get("SMTP_PORT", "587")),
        "user": os.environ.get("SMTP_USER", ""),
        "password": os.environ.get("SMTP_PASS", ""),
        "from_addr": from_addr,
    }


def _send_email_sync(*, to_email: str | None, subject: str, body: str) -> None:
    if not to_email:
        return

    config = _smtp_config()
    if config is None:
        return

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config["from_addr"]
    message["To"] = to_email
    message.set_content(body)

    try:
        with smtplib.SMTP(config["host"], config["port"], timeout=20) as smtp:
            smtp.starttls()
            if config["user"]:
                smtp.login(config["user"], config["password"])
            smtp.send_message(message)
    except Exception as exc:
        logger.error("Failed to send email to %s: %s", to_email, exc)


async def send_purchase_success_email(
    *,
    to_email: str | None,
    domain: str,
    dns_token: str,
    nameservers: list[str],
) -> None:
    nameserver_lines = "\n".join(f"- {nameserver}" for nameserver in nameservers) or "- unavailable"
    body = (
        f"Your domain purchase is complete for {domain}.\n\n"
        "Nameservers:\n"
        f"{nameserver_lines}\n\n"
        "Cloudflare DNS API token:\n"
        f"{dns_token}\n"
    )
    await asyncio.to_thread(
        _send_email_sync,
        to_email=to_email,
        subject=f"InstaDomain purchase complete: {domain}",
        body=body,
    )


async def send_expiration_reminder_email(
    *,
    to_email: str | None,
    domain: str,
    expires_at: str,
    days_remaining: int,
    renewal_url: str,
) -> None:
    """Send a domain expiration reminder email."""
    body = (
        f"Your domain {domain} expires on {expires_at} "
        f"({days_remaining} days from now).\n\n"
        "To renew your domain, use this link:\n\n"
        f"{renewal_url}\n\n"
        "If the domain expires and is not renewed, it may become available "
        "for anyone to register. Please make sure your payment details "
        "are up to date."
    )
    await asyncio.to_thread(
        _send_email_sync,
        to_email=to_email,
        subject=f"InstaDomain: {domain} expires in {days_remaining} days",
        body=body,
    )


ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "nach@nachdakwale.com")


async def send_crypto_refund_alert(
    *,
    order_id: str,
    domain: str,
    amount_usdc: str,
    payer_address: str | None,
    tx_hash: str | None,
    error_msg: str,
) -> None:
    """Send an alert when domain registration fails after an x402 crypto payment.

    This requires manual intervention to refund the payer on-chain.
    """
    payer_info = payer_address or tx_hash or "unknown"
    body = (
        f"CRYPTO REFUND REQUIRED\n\n"
        f"Domain registration failed after on-chain payment.\n\n"
        f"Order ID: {order_id}\n"
        f"Domain: {domain}\n"
        f"Amount: {amount_usdc} USDC\n"
        f"Payer address: {payer_address or 'not captured'}\n"
        f"Transaction hash: {tx_hash or 'not captured'}\n"
        f"Error: {error_msg}\n\n"
        f"Action required: manually refund {amount_usdc} USDC to {payer_info}."
    )
    await asyncio.to_thread(
        _send_email_sync,
        to_email=ALERT_EMAIL,
        subject=f"[ALERT] Crypto refund needed: {domain} (order {order_id})",
        body=body,
    )


async def send_renewal_failure_alert(
    *,
    order_id: str,
    domain: str,
    error_msg: str,
) -> None:
    """Send an alert when a paid domain renewal fails at the registrar.

    A Stripe refund should already have been attempted by the caller.
    This notifies the operator so they can investigate and intervene.
    """
    body = (
        f"RENEWAL FAILURE\n\n"
        f"A customer paid for a domain renewal but the OpenSRS renewal call failed.\n\n"
        f"Order ID: {order_id}\n"
        f"Domain: {domain}\n"
        f"Error: {error_msg}\n\n"
        f"A refund was attempted automatically. Please verify the refund "
        f"succeeded in the Stripe dashboard and investigate the renewal failure."
    )
    await asyncio.to_thread(
        _send_email_sync,
        to_email=ALERT_EMAIL,
        subject=f"[ALERT] Renewal failed: {domain} (order {order_id})",
        body=body,
    )


async def send_refund_email(
    *,
    to_email: str | None,
    domain: str,
    amount_cents: int | None,
) -> None:
    amount_text = f"${amount_cents / 100:.2f}" if amount_cents is not None else "your full payment"
    body = (
        f"We were unable to complete the registration for {domain}.\n\n"
        f"A refund for {amount_text} has been initiated to your original payment method."
    )
    await asyncio.to_thread(
        _send_email_sync,
        to_email=to_email,
        subject=f"InstaDomain refund issued: {domain}",
        body=body,
    )

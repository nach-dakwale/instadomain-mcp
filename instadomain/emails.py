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

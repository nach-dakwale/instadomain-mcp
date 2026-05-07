#!/usr/bin/env python3
"""Upload stripe_product_feed.csv to Stripe's catalog import API.

Two-step Stripe flow:
  1) POST /v2/commerce/product_catalog/imports
       returns an `awaiting_upload` import with a presigned upload URL
  2) PUT the CSV bytes to that URL

Requires INSTADOMAIN_STRIPE_SECRET_KEY in the environment. The script
prints the import id + final status so you can spot validation failures.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx


STRIPE_API = "https://api.stripe.com"
STRIPE_VERSION = "2026-04-22.dahlia"


def main() -> int:
    secret = os.environ.get("INSTADOMAIN_STRIPE_SECRET_KEY") or os.environ.get(
        "STRIPE_SECRET_KEY"
    )
    if not secret:
        print(
            "error: set INSTADOMAIN_STRIPE_SECRET_KEY (or STRIPE_SECRET_KEY)",
            file=sys.stderr,
        )
        return 2

    csv_path = (
        Path(__file__).resolve().parent.parent / "stripe_product_feed.csv"
    )
    if not csv_path.exists():
        print(
            f"error: {csv_path} missing — run generate_product_feed.py first",
            file=sys.stderr,
        )
        return 2

    headers = {
        "Authorization": f"Bearer {secret}",
        "Stripe-Version": STRIPE_VERSION,
    }

    with httpx.Client(timeout=60.0) as http:
        create = http.post(
            f"{STRIPE_API}/v2/commerce/product_catalog/imports",
            headers=headers,
            json={
                "feed_type": "product",
                "mode": "upsert",
                "metadata": {"source": "instadomain-mcp", "rows": "10"},
            },
        )
        if create.status_code >= 300:
            print(f"create failed: {create.status_code}\n{create.text}")
            return 1
        body = create.json()
        import_id = body.get("id")
        upload_url = body.get("upload_url") or (
            body.get("file") or {}
        ).get("upload_url")
        print(f"created import {import_id}")
        print(json.dumps(body, indent=2))

        if not upload_url:
            print("no upload_url in response — check shape above", file=sys.stderr)
            return 1

        put = http.put(
            upload_url,
            content=csv_path.read_bytes(),
            headers={"Content-Type": "text/csv"},
        )
        if put.status_code >= 300:
            print(f"upload failed: {put.status_code}\n{put.text}")
            return 1
        print(f"uploaded {csv_path.stat().st_size} bytes")

        status = http.get(
            f"{STRIPE_API}/v2/commerce/product_catalog/imports/{import_id}",
            headers=headers,
        )
        print(f"status: {status.status_code}")
        print(status.text)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

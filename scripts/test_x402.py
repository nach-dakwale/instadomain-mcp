#!/usr/bin/env python3
"""Standalone test script for x402 testnet payments on Base Sepolia.

Usage:
    # Generate a fresh wallet:
    python scripts/test_x402.py

    # Use an existing wallet:
    EVM_PRIVATE_KEY=0x... python scripts/test_x402.py

Requires:
    - Server running at localhost:8080 with INSTADOMAIN_X402_TESTNET=true
    - eth_account, web3, httpx, x402 packages installed
"""

from __future__ import annotations

import json
import os
import sys
import time

import httpx
from eth_account import Account
from web3 import Web3

BASE_SEPOLIA_RPC = "https://sepolia.base.org"
USDC_ADDRESS = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
SERVER_URL = "http://localhost:8080"

ERC20_BALANCE_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


def get_or_create_wallet() -> Account:
    """Load wallet from EVM_PRIVATE_KEY env var or generate a fresh one."""
    private_key = os.environ.get("EVM_PRIVATE_KEY")
    if private_key:
        account = Account.from_key(private_key)
        print(f"Using existing wallet: {account.address}")
    else:
        account = Account.create()
        print(f"Generated fresh wallet: {account.address}")
        print(f"Private key: {account.key.hex()}")
        print("(Set EVM_PRIVATE_KEY to reuse this wallet)")
    return account


def check_usdc_balance(address: str) -> float:
    """Check USDC balance on Base Sepolia via RPC."""
    w3 = Web3(Web3.HTTPProvider(BASE_SEPOLIA_RPC))
    usdc = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_ADDRESS),
        abi=ERC20_BALANCE_ABI,
    )
    raw_balance = usdc.functions.balanceOf(
        Web3.to_checksum_address(address)
    ).call()
    # USDC has 6 decimals
    return raw_balance / 1_000_000


def create_x402_order(domain: str) -> dict:
    """Call POST /buy/crypto to create an x402 order."""
    payload = {
        "domain": domain,
        "registrant": {
            "first_name": "Test",
            "last_name": "User",
            "email": "test@example.com",
            "address1": "123 Test St",
            "city": "San Francisco",
            "state": "CA",
            "postal_code": "94102",
            "country": "US",
            "phone": "+1.5551234567",
        },
    }
    resp = httpx.post(f"{SERVER_URL}/buy/crypto", json=payload, timeout=30)
    if resp.status_code != 200:
        print(f"FAIL: /buy/crypto returned {resp.status_code}")
        print(resp.text)
        sys.exit(1)
    return resp.json()


def pay_with_x402(account: Account, pay_url: str) -> bool:
    """Hit the pay_url with x402 payment negotiation.

    Flow:
    1. GET the pay_url, expect a 402 with payment requirements
    2. Use x402 client to create a payment payload
    3. Retry the request with X-PAYMENT header
    """
    from x402 import x402Client
    from x402.mechanisms.evm.exact import ExactEvmScheme
    from x402.mechanisms.evm.signers import EthAccountSigner
    from x402.schemas import PaymentRequired

    # Set up x402 client with our wallet
    signer = EthAccountSigner(account)
    client = x402Client()
    client.register("eip155:84532", ExactEvmScheme(signer=signer))

    full_url = f"{SERVER_URL}{pay_url}"

    # Step 1: GET the paywalled endpoint, expect 402
    print(f"\nHitting pay URL: {full_url}")
    resp = httpx.get(full_url, timeout=30)

    if resp.status_code != 402:
        print(f"Expected 402, got {resp.status_code}")
        print(resp.text)
        return False

    # Step 2: Parse 402 response and create payment
    payment_required_data = resp.json()
    print(f"Got 402 response with payment requirements")
    print(f"  Network: {json.dumps(payment_required_data.get('accepts', [{}]), indent=2)[:200]}")

    payment_required = PaymentRequired.model_validate(payment_required_data)

    import asyncio
    payload = asyncio.run(client.create_payment_payload(payment_required))

    # Step 3: Retry with payment header
    print("Sending payment...")
    payment_header = payload.model_dump_json()
    resp2 = httpx.get(
        full_url,
        headers={"X-PAYMENT": payment_header},
        timeout=30,
    )

    if resp2.status_code == 200:
        print(f"SUCCESS: Payment accepted!")
        print(f"Response: {json.dumps(resp2.json(), indent=2)}")
        return True
    else:
        print(f"FAIL: Payment rejected with status {resp2.status_code}")
        print(resp2.text)
        return False


def main():
    print("=== x402 Testnet Payment Test ===\n")

    # Step 1: Wallet setup
    account = get_or_create_wallet()

    # Step 2: Check balance
    balance = check_usdc_balance(account.address)
    print(f"USDC balance on Base Sepolia: {balance:.6f}")

    if balance < 0.01:
        print("\nInsufficient testnet USDC balance.")
        print("Get testnet USDC from the CDP faucet:")
        print(f"  https://portal.cdp.coinbase.com/products/faucet")
        print(f"  Address: {account.address}")
        print(f"  Network: Base Sepolia")
        print("\nRun this script again after funding.")
        sys.exit(1)

    # Step 3: Create an x402 order
    test_domain = os.environ.get("TEST_DOMAIN", "zzqrtx7749.com")
    print(f"\nCreating x402 order for: {test_domain}")
    order = create_x402_order(test_domain)
    print(f"Order created: {order['order_id']}")
    print(f"Price: {order['price_usdc']} USDC ({order['price_display']})")
    print(f"Network: {order['network']}")
    print(f"Asset: {order['asset']}")

    # Step 4: Pay via x402
    success = pay_with_x402(account, order["pay_url"])

    if not success:
        print("\n=== TEST FAILED (payment rejected) ===")
        sys.exit(1)

    # Step 5: Poll order status until terminal state
    print(f"\nPolling order status for {order['order_id']}...")
    order_id = order["order_id"]
    terminal_states = {"complete", "failed"}
    max_polls = 60
    poll_interval = 5

    for i in range(max_polls):
        resp = httpx.get(f"{SERVER_URL}/status/{order_id}", timeout=10)
        if resp.status_code != 200:
            print(f"  Poll {i+1}: HTTP {resp.status_code}")
            time.sleep(poll_interval)
            continue

        status_data = resp.json()
        status = status_data["status"]
        print(f"  Poll {i+1}: {status}")

        if status in terminal_states:
            if status == "complete":
                print(f"\n=== FULL E2E TEST PASSED ===")
                print(f"Domain: {status_data['domain']}")
                print(f"Nameservers: {status_data.get('nameservers')}")
                if status_data.get("cloudflare_token"):
                    print(f"DNS Token: {status_data['cloudflare_token']}")
            else:
                print(f"\n=== TEST FAILED (fulfillment) ===")
                print(f"Error: {status_data.get('error_msg')}")
                sys.exit(1)
            break

        time.sleep(poll_interval)
    else:
        print(f"\n=== TEST TIMED OUT after {max_polls * poll_interval}s ===")
        sys.exit(1)


if __name__ == "__main__":
    main()

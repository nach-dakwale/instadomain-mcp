# x402 E2E Test Procedure

Full end-to-end test: x402 crypto payment -> OpenSRS domain registration -> Cloudflare DNS setup. Uses testnet only, no real money.

## Prerequisites

- PostgreSQL 17 running locally with `instadomain` database
- Python venv at `.venv/` with all deps installed
- Test wallets in `scripts/test-wallets.env` (gitignored)
- OpenSRS test API key (generated at https://horizon.opensrs.net/resellers/)
- At least 14 USDC in the payer wallet on Base Sepolia

## Test wallets

Saved in `scripts/test-wallets.env`. Both private keys are known.

| Role | Address | Key env var |
|------|---------|-------------|
| Payer | `0x3a7D45FEB568De70EFBcc9e73fa9e1d10bdBB1FC` | `TEST_PAYER_PRIVATE_KEY` |
| Server | `0x0C21F6e6d19EfEE98C241460a973CCe042425629` | `TEST_SERVER_PRIVATE_KEY` |

To fund the payer: go to https://faucet.circle.com/, select Base Sepolia, paste the payer address, solve CAPTCHA. Gives 20 USDC per request (2hr cooldown per address).

After each test the USDC moves from payer to server. To reuse funds, swap wallets: set `X402_WALLET_ADDRESS` to the payer address and `EVM_PRIVATE_KEY` to the server key.

## 1. Start services

```bash
brew services start postgresql@17
```

## 2. Start the server

```bash
cd ~/projects/instadomain

INSTADOMAIN_X402_TESTNET=true \
INSTADOMAIN_X402_WALLET_ADDRESS=0x0C21F6e6d19EfEE98C241460a973CCe042425629 \
INSTADOMAIN_DATABASE_URL=postgresql://localhost/instadomain \
INSTADOMAIN_ENCRYPTION_KEY='98yN81xcq7JAmVb7vcVTLFtIAPQDUB0SDkfKpjX0QHY=' \
INSTADOMAIN_OPENSRS_TEST_API_KEY=<test key from Horizon> \
INSTADOMAIN_OPENSRS_RESELLER_USERNAME=nachdakwale \
INSTADOMAIN_CLOUDFLARE_API_TOKEN=<from Fly secrets> \
INSTADOMAIN_CLOUDFLARE_ACCOUNT_ID=<from Fly secrets> \
.venv/bin/python -m uvicorn instadomain.api:create_app \
  --host 0.0.0.0 --port 8080 --factory
```

Testnet mode auto-switches to:
- OpenSRS test API at `https://horizon.opensrs.net:55443`
- x402 on Base Sepolia (chain 84532)
- Free x402 facilitator at `https://x402.org/facilitator`

## 3. Run the test

```bash
EVM_PRIVATE_KEY=129e9510a66532aa90f3b2077e943f7c0dff922d498567df1ef66cbd432c2124 \
TEST_DOMAIN=<any-unused-domain>.com \
.venv/bin/python scripts/test_x402.py
```

Pick a random domain name each run. OpenSRS test env has $5k fake credits.

## 4. What the test does

1. Checks payer USDC balance on Base Sepolia
2. `POST /buy/crypto` with domain + registrant contact -> gets order ID + pay URL
3. `GET /pay/{id}` -> 402 with payment requirements
4. Signs x402 payment with payer wallet, retries with `X-PAYMENT` header -> 200
5. Polls `GET /status/{id}` every 5s until terminal state
6. Expected transitions: `pending_payment` -> `registering` -> `setting_dns` -> `complete`

## 5. Verify the results

After the test prints `FULL E2E TEST PASSED`, verify each claim:

### Domain registered at OpenSRS

```python
from instadomain.opensrs_client import OpenSRSClient
c = OpenSRSClient(api_key='<test key>', reseller_username='nachdakwale',
                  api_url='https://horizon.opensrs.net:55443')
print(c.check_availability('<domain>'))  # Should be False
```

### Nameservers set to Cloudflare (not placeholders)

```python
attrs = {'domain': '<domain>', 'type': 'nameservers'}
xml = c._build_envelope('GET', 'DOMAIN', attrs)
resp = c._post(xml)
data = c._parse_response(resp.text)
print(data['attributes']['nameserver_list'])
# Should show *.ns.cloudflare.com, NOT ns1.instadomain.dev
```

### Cloudflare zone exists and DNS token works

```bash
curl -s -H "Authorization: Bearer <dns_token_from_test_output>" \
  "https://api.cloudflare.com/client/v4/zones?name=<domain>" | python3 -m json.tool
# Should show zone with nameservers matching
```

### USDC transferred on-chain

```python
from web3 import Web3
w3 = Web3(Web3.HTTPProvider('https://sepolia.base.org'))
USDC = '0x036CbD53842c5426634e7929541eC2318f3dCF7e'
abi = [{'inputs':[{'name':'account','type':'address'}],'name':'balanceOf',
        'outputs':[{'name':'','type':'uint256'}],'stateMutability':'view','type':'function'}]
usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC), abi=abi)
bal = usdc.functions.balanceOf(Web3.to_checksum_address('<server_address>')).call()
print(f'{bal / 1_000_000} USDC')  # Should show 13.56
```

## 6. Clean up

```bash
# Kill the server (Ctrl+C or kill PID)
brew services stop postgresql@17
```

## Reusing USDC across tests

Each test moves ~13.56 USDC from payer to server. To run again without refunding:

**Swap wallets:** Set `INSTADOMAIN_X402_WALLET_ADDRESS` to the current payer address, and `EVM_PRIVATE_KEY` to the current server private key. The roles flip and the money flows back.

**Or refund from Circle faucet:** https://faucet.circle.com/ - 20 USDC per request, 2hr cooldown per address. New addresses have no cooldown.

## OpenSRS test API key

Generated at https://horizon.opensrs.net/resellers/ -> "Generate New Private Key". Login: `nachdakwale`. Key activates 3 minutes after generation. Test env has $5k fake credits, domains don't resolve on the real internet.

## Known behaviors

- Nameserver update: the flow unlocks the domain, updates NS to Cloudflare, re-locks. The "non-fatal" warning in logs for the unlock/lock steps is expected.
- OpenSRS test domains use `ns1.systemdns.com`/`ns2.systemdns.com` as registry defaults but we override to Cloudflare.
- Cloudflare zones show `status: pending` because the test domains never actually delegate to Cloudflare on the real internet.

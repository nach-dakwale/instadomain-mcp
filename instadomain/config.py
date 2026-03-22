from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "INSTADOMAIN_"}

    opensrs_api_key: str = ""
    opensrs_reseller_username: str = ""
    opensrs_api_url: str = "https://rr-n1-tor.opensrs.net:55443"

    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""

    cloudflare_api_token: str = ""
    cloudflare_account_id: str = ""

    database_url: str = "postgresql://localhost/instadomain"
    encryption_key: str = ""

    api_host: str = "0.0.0.0"
    api_port: int = 8080
    backend_url: str = "https://instadomain.dev/api"

    standard_markup_cents: int = 210
    premium_markup_pct: float = 0.25

    # x402 crypto payments (USDC on Base)
    x402_wallet_address: str = ""
    x402_facilitator_url: str = "https://facilitator.xpay.sh"
    x402_network: str = "eip155:8453"

    x402_testnet: bool = False

    # Affiliate links
    dynadot_affiliate_id: str = "PLACEHOLDER_DYNADOT"
    enable_affiliate_links: bool = True

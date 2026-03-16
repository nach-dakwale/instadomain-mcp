"""
config.py - Application configuration via environment variables / .env file.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Affiliate ID (set via env var or .env)
    dynadot_affiliate_id: str = "PLACEHOLDER_DYNADOT"

    # Database
    db_path: str = "./data/analytics.db"

    # RDAP settings
    rdap_timeout: float = 10.0
    max_concurrent_lookups: int = 10

    # API server
    api_host: str = "0.0.0.0"
    api_port: int = 8080

    # Feature flags
    enable_analytics: bool = True
    enable_affiliate_links: bool = True


settings = Settings()

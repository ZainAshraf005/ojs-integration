from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Core
    database_url: str = "postgresql+asyncpg://archive:archive@localhost:5432/archive"
    redis_url: str = "redis://localhost:6379/0"
    app_secret_key: str  # signs sessions and encrypts stored OJS API tokens
    public_base_url: str = "http://localhost:8000"  # e.g. https://doi-services.com
    site_name: str = "DOI Services"

    # Storage
    storage_dir: Path = Path("data/files")
    max_file_bytes: int = 200 * 1024 * 1024

    # Talking to OJS politely (Hostinger shared hosting has resource limits)
    ojs_request_delay: float = 0.5  # seconds between requests per worker job
    ojs_timeout: float = 60.0
    user_agent: str = "DOI-Services/0.1 (+https://doi-services.com)"

    # Webhooks
    webhook_max_skew: int = 300  # seconds a signed webhook stays valid
    webhook_sync_delay: int = 15  # wait before fetching, so OJS has finished its own writes

    # ?redirect links: the journal's page is checked live (result cached) before sending readers there
    redirect_check_timeout: float = 4.0
    redirect_check_ttl: int = 120  # seconds

    # Health checks
    health_article_batch: int = 50  # articles re-checked per source per run
    health_article_max_age_hours: int = 6

    # Off-site replica (Backblaze B2 / any S3-compatible). Leave empty to disable.
    s3_endpoint_url: str = ""
    s3_bucket: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_region: str = ""

    @property
    def s3_enabled(self) -> bool:
        return bool(self.s3_bucket and self.s3_access_key and self.s3_secret_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()

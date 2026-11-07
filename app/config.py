from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Forge Media API"
    database_url: str = "sqlite:///./data/forge.db"
    local_storage_path: str = "./data/storage"
    redis_url: str = "redis://localhost:6379/0"
    dispatch_interval_seconds: int = 30
    job_timeout_seconds: int = 600
    job_recovery_grace_seconds: int = 120
    job_max_attempts: int = Field(default=3, ge=1)
    job_retry_base_seconds: int = Field(default=30, ge=1)
    job_retry_max_seconds: int = Field(default=300, ge=1)
    webhook_master_secret: SecretStr = SecretStr("")

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()


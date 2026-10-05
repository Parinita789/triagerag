from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")
    database_url: str
    openrouter_api_key: str = ""
    openrouter_model: str = ""
    data_dir: Path = Path(__file__).resolve().parents[2] / "data"
    jira_webhook_secret: str = ""
    judge_model: str = ""
    loki_url: str = "http://localhost:3100"
    api_key: str = ""  # X-API-Key for /jobs and /metrics; unset = those endpoints refuse every request


settings = Settings()

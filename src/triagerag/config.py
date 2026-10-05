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


settings = Settings()

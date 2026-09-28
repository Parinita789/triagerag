from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")
    database_url: str
    data_dir: Path = Path(__file__).resolve().parents[2] / "data"


settings = Settings()

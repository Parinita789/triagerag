from pathlib import Path
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


BACKEND = Path(__file__).resolve().parents[2]   # backend/
REPO = BACKEND.parent


class Settings(BaseSettings):
    # backend/.env, wherever the command is run from; in Docker the variables come from compose
    model_config = SettingsConfigDict(env_file=BACKEND / ".env")
    database_url: str
    openrouter_api_key: str = ""
    openrouter_model: str = ""
    data_dir: Path = BACKEND / "data"
    jira_webhook_secret: str = ""
    judge_model: str = ""
    loki_url: str = "http://localhost:3100"
    preview_rerank: bool = True  # run the reranker in triage jobs for display (not used for ranking)
    ui_dist: Path = REPO / "frontend" / "dist"  # the built dashboard, served at /ui
    api_key: str = ""  # X-API-Key for /jobs and /metrics; unset = those endpoints refuse every request

    @field_validator("data_dir", "ui_dist")
    @classmethod
    def _relative_to_backend(cls, path: Path) -> Path:
        """DATA_DIR=./data means backend/data, not ./data of whatever folder a command runs in."""
        return path if path.is_absolute() else (BACKEND / path).resolve()


settings = Settings()

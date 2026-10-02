import hashlib
import json
import time
from pathlib import Path
from typing import Any

from openai import APIConnectionError, APIStatusError, OpenAI, RateLimitError

CACHE_DIR = Path("data/llm_cache")
BASE_URL = "https://openrouter.ai/api/v1"
REQUEST_TIMEOUT = 90   # seconds; free models sometimes stall


class LLMClient:
    def __init__(self, api_key: str, model: str, use_cache: bool = True) -> None:
        if not api_key or not model:
            raise ValueError("set OPENROUTER_API_KEY and OPENROUTER_MODEL in .env")
        self.client = OpenAI(base_url=BASE_URL, api_key=api_key,
                            timeout=REQUEST_TIMEOUT, max_retries=0)
        self.model = model
        self.use_cache = use_cache
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def chat(self, messages: list[dict], tools: list[dict]) -> dict[str, Any]:
        payload = {
            "model": self.model, "messages": messages, "tools": tools, "temperature": 0,
            "max_tokens": 4096,
            "extra_body": {"reasoning": {"effort": "low"}},
        }
        key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        path = CACHE_DIR / f"{key}.json"
        if self.use_cache and path.exists():
            return json.loads(path.read_text())

        for attempt in range(1, 5):
            try:
                data = self.client.chat.completions.create(**payload).model_dump()
                if data["choices"][0].get("finish_reason") != "error":
                    path.write_text(json.dumps(data))
                return data  # finish_reason == "error": returned but not cached
            except (RateLimitError, APIConnectionError) as e:
                if attempt == 4:
                    raise
                reason = type(e).__name__
            except APIStatusError as e:
                if e.status_code < 500 or attempt == 4:
                    raise
                reason = f"HTTP {e.status_code}"
            wait = 15 * attempt
            print(f"    LLM retry {attempt}/3 after {reason}, waiting {wait}s", flush=True)
            time.sleep(wait)
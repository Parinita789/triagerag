"""Free OpenRouter models that support tool calling, excluding the agent's own family (Qwen)."""
import httpx

from triagerag.config import settings

r = httpx.get("https://openrouter.ai/api/v1/models",
              headers={"Authorization": f"Bearer {settings.openrouter_api_key}"}, timeout=30)
r.raise_for_status()

rows = []
for m in r.json()["data"]:
    p = m.get("pricing") or {}
    free = str(p.get("prompt")) == "0" and str(p.get("completion")) == "0"
    tools = "tools" in (m.get("supported_parameters") or [])
    if free and tools and "qwen" not in m["id"].lower():
        rows.append((m["id"], m.get("context_length") or 0))

for model_id, ctx in sorted(rows):
    print(f"{model_id:<60} context {ctx:>9,}")
print(f"\n{len(rows)} free models with tool calling (Qwen excluded)")
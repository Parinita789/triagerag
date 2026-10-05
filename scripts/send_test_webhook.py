"""Send a signed fake 'issue created' webhook twice: expect queued (202), then duplicate (200)."""
import hashlib
import hmac
import json
import sys

import httpx

from triagerag.config import settings

key = sys.argv[1] if len(sys.argv) > 1 else "TEST-1"
event = {
    "webhookEvent": "jira:issue_created",
    "issue": {"key": key, "fields": {
        "summary": "DataNode fails to report blocks after restart",
        "created": "2026-10-02T12:00:00.000-0700",
        "description": {"type": "doc", "content": [{"type": "paragraph", "content": [
            {"type": "text", "text": "After restarting dn3, the NameNode marks its blocks as missing. "
                                     "Contact me at alice@corp.com, token=AKIA1234567890ABCDEF"}]}]},
    }},
}
body = json.dumps(event).encode()
sig = "sha256=" + hmac.new(settings.jira_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
for attempt in (1, 2):
    r = httpx.post("http://localhost:8000/webhooks/jira", content=body, headers={"X-Hub-Signature": sig})
    print(f"send {attempt}: {r.status_code} {r.text[:300]}")
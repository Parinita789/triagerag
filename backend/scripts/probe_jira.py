import json
from collections import Counter

BOTS = {"hudson", "hadoopqa", "githubbot", "genericqa"}

data = json.load(open("data/jira/probe.json"))
issues = data["issues"]
n = len(issues)

with_links = 0
link_types: Counter[str] = Counter()
with_code = 0
comments_total = comments_bot = 0
truncated = 0

for it in issues:
    f = it["fields"]

    links = f.get("issuelinks") or []
    if links:
        with_links += 1
    for l in links:
        link_types[l["type"]["name"]] += 1

    desc = f.get("description") or ""
    if "{code" in desc or "{noformat" in desc:
        with_code += 1

    c = f.get("comment") or {}
    cs = c.get("comments", [])
    if c.get("total", 0) > len(cs):
        truncated += 1
    for cm in cs:
        comments_total += 1
        if cm["author"]["name"] in BOTS:
            comments_bot += 1

print(f"tickets:              {n}")
print(f"with issue links:     {with_links} ({with_links / n:.0%})")
print(f"link types:           {dict(link_types)}")
print(f"desc with code/logs:  {with_code} ({with_code / n:.0%})")
print(f"comments:             {comments_total}, from bots: {comments_bot} ({comments_bot / max(comments_total, 1):.0%})")
print(f"tickets w/ truncated comments: {truncated}")
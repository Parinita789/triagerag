"""Compare Loki's line counts with the file, per stream and per hour."""
import httpx

LOKI = "http://localhost:3100"
END = 1226404800  # 2008-11-11 12:00 UTC

c = httpx.Client(timeout=120)

r = c.get(f"{LOKI}/loki/api/v1/query", params={
    "query": 'sum by (component, level) (count_over_time({level=~".+"}[40h]))', "time": END})
print("per stream (one 40h window):")
total = 0
for s in sorted(r.json()["data"]["result"], key=lambda s: -int(s["value"][1])):
    n = int(s["value"][1]); total += n
    print(f"  {s['metric']['component']:<40} {s['metric']['level']:<6} {n:>10,}")
print(f"  total {total:,}")

r = c.get(f"{LOKI}/loki/api/v1/query_range", params={
    "query": 'sum(count_over_time({level=~".+"}[1h]))',
    "start": END - 40 * 3600 + 3600, "end": END, "step": 3600})
hourly = sum(int(v) for _, v in r.json()["data"]["result"][0]["values"])
print(f"\nsum of 40 one-hour windows: {hourly:,}")
print("file:                       11,175,629")
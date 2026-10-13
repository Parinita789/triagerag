#!/usr/bin/env bash
set -u
L=localhost:3100
S=1226262000000000000
E=1226263000000000000

until curl -s $L/ready | grep -q '^ready'; do sleep 3; done
echo "1. READY"

echo "2. config:"
curl -s $L/config | grep -E "query_ingesters_within|reject_old_samples"

echo "3. push:"
curl -s -w "status %{http_code}\n" -X POST $L/loki/api/v1/push \
  -H 'Content-Type: application/json' \
  -d '{"streams":[{"stream":{"component":"test","level":"INFO"},"values":[["1226262918000000000","hello from 2008"]]}]}'
sleep 3

echo "4. series:"
curl -sG $L/loki/api/v1/series --data-urlencode 'match[]={component="test"}' \
  --data-urlencode start=$S --data-urlencode end=$E
echo

echo "5. query:"
curl -sG $L/loki/api/v1/query_range --data-urlencode 'query={component="test"}' \
  --data-urlencode start=$S --data-urlencode end=$E \
  | python3 -c "import json,sys; d=json.load(sys.stdin)['data']; print('result:', d['result']); print('ingester reached:', d['stats']['ingester']['totalReached'])"
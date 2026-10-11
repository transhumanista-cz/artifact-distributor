#!/usr/bin/env bash
# Wires a REAL Harbor to the distributor (not needed for the compose PoC):
#   1. read-only robot account on project acme.co -> stored in Vault/OpenBao for the distributor
#   2. webhook policy: PUSH_ARTIFACT -> distributor, authenticated with a shared secret
# Uses Harbor API v2.0. Requires: curl, jq, bao/vault CLI logged in with write access.
set -euo pipefail
: "${HARBOR_URL:?https://harbor.acme.internal}" "${HARBOR_ADMIN_USER:?}" "${HARBOR_ADMIN_PASSWORD:?}"
: "${DISTRIBUTOR_WEBHOOK:=https://artifact-distributor.acme.internal/webhooks/harbor}"
: "${WEBHOOK_SECRET:?shared secret, same value as the distributor's WEBHOOK_SECRET}"
PROJECT="${PROJECT:-acme.co}"
VAULT="${VAULT:-bao}"
HOST="${HARBOR_URL#https://}"
api() { curl -sSf -u "$HARBOR_ADMIN_USER:$HARBOR_ADMIN_PASSWORD" -H 'Content-Type: application/json' -H 'X-Is-Resource-Name: true' "$@"; }

robot=$(api -X POST "$HARBOR_URL/api/v2.0/robots" -d @- <<JSON
{"name": "artifact-distributor", "duration": -1, "level": "project",
 "description": "read-only, used by artifact-distributor",
 "permissions": [{"kind": "project", "namespace": "$PROJECT",
                  "access": [{"resource": "repository", "action": "pull"},
                             {"resource": "artifact", "action": "read"},
                             {"resource": "tag", "action": "list"}]}]}
JSON
)
$VAULT kv put "secret/registries/$HOST/$PROJECT" \
  username="$(jq -r .name <<<"$robot")" password="$(jq -r .secret <<<"$robot")"

api -X POST "$HARBOR_URL/api/v2.0/projects/$PROJECT/webhook/policies" -d @- <<JSON
{"name": "artifact-distributor", "enabled": true, "event_types": ["PUSH_ARTIFACT"],
 "targets": [{"type": "http", "address": "$DISTRIBUTOR_WEBHOOK", "auth_header": "$WEBHOOK_SECRET",
              "skip_cert_verify": false, "payload_format": "Default"}]}
JSON
echo "Harbor project $PROJECT wired to $DISTRIBUTOR_WEBHOOK"

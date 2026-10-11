#!/usr/bin/env bash
# Runs the whole PoC WITHOUT Docker: zot (x3), OpenBao dev server and the distributor as
# local processes, then the end-to-end scenario. Needs `zot`, `bao` and `regctl` on PATH
# (single static binaries from their GitHub releases).
#
#   ./poc/run-local.sh            # run scenario, stop everything afterwards
#   KEEP=1 ./poc/run-local.sh     # leave the stack running for poking around
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${WORK:-$(mktemp -d /tmp/distributor-poc.XXXX)}"
export HARBOR=localhost:5001 CUSTOMER=localhost:5002 PUBLIC=localhost:5003
export VAULT_ADDR=http://127.0.0.1:8200 VAULT_TOKEN=root BAO_ADDR=http://127.0.0.1:8200 BAO_TOKEN=root
export WEBHOOK_SECRET=poc-webhook-secret API_TOKEN=poc-api-token
export DISTRIBUTOR_CONFIG="$ROOT/poc/platform.yaml" STATE_PATH="$WORK/state.db" PORT=8080
export GITOPS_DIR="$WORK/gitops" POLL_SECONDS=5 RECONCILE_SECONDS=300
export DISTRIBUTOR_URL=http://127.0.0.1:8080 SEED_REGISTRY=$HARBOR
export SEED_WEBHOOK=http://127.0.0.1:8080/webhooks/harbor
cp -r "$ROOT/poc/gitops/repos" "$GITOPS_DIR"   # the scenario edits a config file

pids=()
cleanup() { [ -n "${KEEP:-}" ] && { echo "stack left running, workdir $WORK"; return; }; kill "${pids[@]}" 2>/dev/null || true; }
trap cleanup EXIT

for spec in harbor:5001 customer:5002 public:5003; do
  n=${spec%%:*}; p=${spec##*:}; mkdir -p "$WORK/$n"
  python3 - "$ROOT/poc/zot/$n.json" "$WORK/$n" "$p" "$ROOT/poc/zot/htpasswd" <<'EOF'
import json, sys
src, work, port, htpasswd = sys.argv[1:]
c = json.load(open(src))
c["storage"]["rootDirectory"] = f"{work}/data"
c["http"].update(address="127.0.0.1", port=port)
c["http"]["auth"]["htpasswd"]["path"] = htpasswd
json.dump(c, open(f"{work}/config.json", "w"))
EOF
  zot serve "$WORK/$n/config.json" >"$WORK/$n.log" 2>&1 & pids+=($!)
done
bao server -dev -dev-root-token-id=root -dev-listen-address=127.0.0.1:8200 >"$WORK/bao.log" 2>&1 & pids+=($!)
BAO=bao "$ROOT/poc/openbao/seed.sh" >/dev/null

cd "$ROOT"
python3 -m distributor serve >"$WORK/distributor.log" 2>&1 & pids+=($!)
rc=0; python3 tests/e2e.py || rc=$?
echo "distributor log: $WORK/distributor.log"
exit $rc

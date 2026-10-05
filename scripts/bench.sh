#!/usr/bin/env bash
# One bench run: drive kohaku-cli through the proxy, replay the traffic at N
# parallel wallets (PIR proxy vs plain RPC), load-test PIR directly, report.
#   scripts/bench.sh               new run under runs/<utc-timestamp>
#   RUN=runs/X STAGES=report ...   re-run stages on an existing run
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD
[[ -f bench.env ]] || { echo "missing bench.env (cp bench.env.example bench.env)"; exit 2; }
# bench.env fills in what the environment does not set (env wins).
while IFS= read -r line; do
  [[ $line =~ ^([A-Z_][A-Z0-9_]*)=(.*)$ ]] || continue
  k=${BASH_REMATCH[1]}; v=${BASH_REMATCH[2]}
  if [[ -z ${!k+x} ]]; then export "$k=$v"; fi
done < bench.env
: "${PIR_URL:?}" "${ETH_RPC_URL:?}"
STAGES=${STAGES:-drive,replay,load,report}
PORT=${PORT:-18545}
PROXY_URL=http://127.0.0.1:$PORT
NETWORK=${NETWORK:-mainnet}
PROXY_EXTRA=()
DRIVE_EXTRA=()
if [[ $NETWORK == sepolia ]]; then
  # The PIR server holds mainnet state: shadow mode keeps PIR timing but
  # answers from the Sepolia RPC. Sends use the persistent funded wallet.
  ETH_RPC_URL=${SEPOLIA_RPC_URL:-https://ethereum-sepolia-rpc.publicnode.com}
  PROXY_EXTRA=(--shadow-pir)
  DRIVE_EXTRA=(--network sepolia --wallet-dir "${SEPOLIA_WALLET_DIR:-runs/_wallets/sepolia}" --wallet-name sepolia-bench)
fi
RUN=${RUN:-runs/$(date -u +%Y%m%dT%H%M%SZ)}
mkdir -p "$RUN"
PROXY_BIN=vendor/local-pir-rpc/target/release/local-pir-rpc
PIR_BENCH=pir-bench/target/release/pir-bench
has() { [[ ",$STAGES," == *",$1,"* ]]; }

PROXY_PID=
start_proxy() { # $1 events file
  if curl -fs -o /dev/null -m 1 "$PROXY_URL" -d '{}' 2>/dev/null; then
    echo "port $PORT is already in use; stop it or set PORT"; exit 1
  fi
  "$PROXY_BIN" --listen "127.0.0.1:$PORT" --pir-url "$PIR_URL" --rpc-url "$ETH_RPC_URL" \
    --events "$1" --record-params --labels bench/labels.json "${PROXY_EXTRA[@]}" >>"$RUN/proxy.log" 2>&1 &
  PROXY_PID=$!
  for _ in $(seq 60); do
    curl -fs -o /dev/null -m 1 "$PROXY_URL/_bench/session" -H 'content-type: application/json' -d '{"session":""}' && return 0
    kill -0 "$PROXY_PID" 2>/dev/null || { echo "proxy exited; see $RUN/proxy.log"; exit 1; }
    sleep 0.5
  done
  echo "proxy did not come up"; exit 1
}
stop_proxy() {
  if [[ -n $PROXY_PID ]]; then kill "$PROXY_PID" 2>/dev/null || true; wait "$PROXY_PID" 2>/dev/null || true; fi
  PROXY_PID=
}
trap stop_proxy EXIT

NETWORK=$NETWORK python3 - "$RUN/meta.json" <<PY
import json, os, subprocess, sys, time, urllib.parse
u = urllib.parse.urlsplit(os.environ["ETH_RPC_URL"])
rev = lambda d: subprocess.run(["git", "-C", d, "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
path = sys.argv[1]
meta = json.load(open(path)) if os.path.exists(path) else {}
meta.update({
    "started_utc": meta.get("started_utc") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "host": os.uname().nodename,
    "pir_url": os.environ["PIR_URL"],
    "fallback_rpc": f"{u.scheme}://{u.hostname}",  # path/query dropped: may carry a key
    "stages": os.environ.get("STAGES", ""),
    "network": os.environ.get("NETWORK", "mainnet"),
    "label": os.environ.get("RUN_LABEL", meta.get("label", "")),
    "note": os.environ.get("RUN_NOTE", meta.get("note", "")),
    "kohaku-cli": rev("vendor/kohaku-cli"), "local-pir-rpc": rev("vendor/local-pir-rpc"),
    "kohaku-rs": rev("vendor/kohaku-rs"), "inspire-gpu-serving": rev("vendor/inspire-gpu-serving"),
})
json.dump(meta, open(path, "w"), indent=1)
PY

if has drive; then
  echo "== drive -> $RUN"
  start_proxy "$RUN/events.jsonl"
  args=(--run-dir "$RUN" --proxy "$PROXY_URL" --fresh "${FRESH:-5}" --timeout "${SCENARIO_TIMEOUT:-1500}")
  [[ -n ${SCENARIOS:-} ]] && args+=(--scenarios "$SCENARIOS")
  [[ ${TOR:-0} == 1 ]] && args+=(--tor)
  args+=("${DRIVE_EXTRA[@]}")
  python3 bench/drive.py "${args[@]}" || echo "   (some scenarios failed; see $RUN/drive/)"
  stop_proxy
fi

if has replay; then
  echo "== replay"
  start_proxy "$RUN/events-replay.jsonl"
  python3 bench/replay.py --events "$RUN/events.jsonl" --target "$PROXY_URL" --label pir --tag-sessions \
    --concurrency "${REPLAY_CONCURRENCY:-1,4,16}" --exclude "${REPLAY_EXCLUDE:-}" --out "$RUN/replay.jsonl"
  stop_proxy
  python3 bench/replay.py --events "$RUN/events.jsonl" --target "$ETH_RPC_URL" --label baseline \
    --concurrency "${REPLAY_CONCURRENCY:-1,4,16}" --exclude "${REPLAY_EXCLUDE:-}" --out "$RUN/replay.jsonl"
fi

if has load; then
  echo "== load (direct PIR)"
  "$PIR_BENCH" --pir-url "$PIR_URL" --concurrency "${LOAD_CONCURRENCY:-1,2,4,8,16}" \
    --lookups "${LOAD_LOOKUPS:-5}" --probes "${PROBES:-20}" --out "$RUN/pir-bench.jsonl"
fi

if has report; then
  echo "== report"
  python3 bench/report.py "$RUN"
  scripts/serve-reports.sh || true
fi

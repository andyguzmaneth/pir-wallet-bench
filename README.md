# pir-wallet-bench

This harness measures what PIR costs a real wallet. It drives kohaku-cli
through the Kohaku `local-pir-rpc` proxy. PIR serves latest-block ETH balance
and nonce, and a normal RPC serves everything else. The harness records every
request and then writes one HTML report.

```
make setup     # clone pinned upstreams into vendor/, apply patches/, build (once)
cp bench.env.example bench.env   # set ETH_RPC_URL
make bench     # new run under runs/<utc-timestamp>/, ends with report.html
```

To re-run stages on an existing run, use `RUN=runs/<id> STAGES=report scripts/bench.sh`.
Variables in the environment override `bench.env`.

## What a run does

| Stage | Tool | Output |
|---|---|---|
| drive | `bench/drive.py` | Throwaway unfunded mainnet wallet. Runs the kohaku-cli scenarios through the proxy, and each scenario tags its own requests. Writes `events.jsonl`, `drive.jsonl` and `drive/*.log`. |
| replay | `bench/replay.py` | Replays the recorded sessions at 1, 4 and 16 parallel wallets. It runs once through the proxy (`pir`) and once direct to the plain RPC (`baseline`). Writes `replay.jsonl` and `events-replay.jsonl`. |
| load | `pir-bench` (Rust) | Direct PIR load test. Each worker has its own `PirClient`, so the result shows the server and network without the proxy lock. Writes `pir-bench.jsonl`. |
| report | `bench/report.py` | Writes `report.html` (self-contained, light and dark) and `summary.json`. |

Scenarios: `create_wallet`, `fresh_addresses`, `balances_public`,
`balances_stealth`, `balances_tornado`, `balances_privacy_pools`,
`balances_railgun`, `balances_all_warm`. Set `SCENARIOS=` in `bench.env` to
choose. `bench/drive.py --help` lists them.

## What the proxy records per request

The proxy writes one JSON line for each JSON-RPC item. The line holds these fields:

- `session`, `seq`, `batch_id`/`batch_size`, `method`, `route` (`AccountBalance`,
  `AccountNonce`, `Call`, `Fallback`), `block_tag`, `ok`/`error`
- `total_ms`, `inflight_at_start`, `rpc_req_bytes`/`rpc_resp_bytes` (the
  JSON-RPC payload, which is what a plain RPC would carry)
- `call`: `to`, contract label, `selector`, function name and kind. Multicall
  batches are decoded into `inner` calls.
- `logs`: addresses, labels, block span, `topic0`. `account`: the queried address.
- `head_block` (from `eth_blockNumber` results). This value gives the PIR
  snapshot lag without extra traffic.
- `pir` (PIR routes only): `lock_wait_ms`, `query_build_ms`, `http_ms`,
  `body_ms`, `decode_ms`, wire `req_bytes`/`resp_bytes`, `sidecar_bytes`,
  `sidecar_entries`, `stash_entries`, `snapshot_block`, `source`
  (`sidecar`/`snapshot`/`stash`/`miss`), `retried`
- `params` (with `--record-params`). The replay needs these.

## Patches

`vendor/` holds pinned upstream checkouts. The bench changes are in `patches/`.
After you edit `vendor/`, run `make patches` to regenerate them.

| Repo | Change |
|---|---|
| `inspire-gpu-serving` (client) | `PirClient::last_metrics`: per-lookup phase timings, wire bytes, sidecar size and answer source. No API change. |
| `kohaku-rs` (`pir-provider`) | `LookupBackend::lookup_traced` (default method) and `PirRouter::request_traced`. The existing `request` is unchanged, and all 11 tests pass. |
| `local-pir-rpc` | `--events`, `--record-params`, `--labels`, `--datasets`, `--session`. Adds `POST /_bench/session`, the `x-bench-session` header, and `src/trace.rs` (selector table, contract labels, Multicall decoding). With no `--events`, the proxy works as before. |

The patches are local. Nothing is pushed upstream.

## Notes

- The PIR request has a fixed shape, so the lookup cost does not depend on the
  key. Lookups for unfunded fresh addresses are PIR misses, but they measure
  the same cost as hits.
- kohaku-cli sends RPC over clearnet. Tor carries only non-RPC HTTP, which
  never reaches the proxy. The driver sets `KOHAKU_WITHOUT_TOR=1` unless
  `TOR=1` is set.
- The server holds only nonzero-balance EOAs. A miss therefore returns 0. This
  is known, and the server will change.
- `bench/labels.json` names contracts in the report. Unknown selectors show as
  hex. Add signatures to `SIGNATURES` in `vendor/local-pir-rpc/src/trace.rs`.
- Some hosts put a non-compiler `cc` on `PATH`. `setup.sh` and the Makefile then
  fall back to `gcc`.
- `PIR_CLIENT_REUSE=1` makes the patched PIR client reuse one connection for
  all lookups instead of opening a new one per lookup. `pir-bench --probes N`
  times each step of a lookup (round trip, upload, server, download) by pairing
  real lookups with probes that upload a full-size body the server rejects
  without GPU work.
- Keep `bench.env` and `runs/` local. Runs hold wallet addresses and full request
  params, and `bench.env` can hold an RPC key.

## Viewing reports

`scripts/serve-reports.sh` runs after each `make bench`. It copies each
`report.html` into two folders. The folders hold report pages only, never wallet
data.

- `runs/_site/`: the full copy, served on the machine's Tailscale address at
  port 8790 (tailnet only).
- `runs/_public/`: the same pages with IP addresses replaced, served on
  127.0.0.1:8791. To publish it, put Tailscale Funnel in front of it:
  `sudo tailscale funnel --bg --https=10000 http://127.0.0.1:8791`.

Funnel opens a whole port. Use a port that carries no other service. To stop
it, run `sudo tailscale funnel --https=10000 off`.

## Reproducing a run

1. `make setup` clones the pinned versions of kohaku-cli, local-pir-rpc,
   kohaku-rs (`experiments/pir-v1`) and inspire-gpu-serving, applies
   `patches/`, and builds.
2. Copy `bench.env.example` to `bench.env`. Set `PIR_URL` to an
   inspire-gpu-serving endpoint and `ETH_RPC_URL` to a mainnet RPC with archive
   access.
3. Run `make bench`. The run directory gets the report, the raw events, and
   the commit of each codebase.

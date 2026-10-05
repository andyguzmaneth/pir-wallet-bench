#!/usr/bin/env python3
"""Replay recorded wallet traffic against a JSON-RPC target at N parallel wallets.

Input is the events.jsonl a drive run produced (proxy started with
--record-params). Each simulated wallet replays every recorded session in
order, one request at a time, the way it was captured. Run it once against
the bench proxy (PIR path) and once against the plain upstream RPC (baseline)
to get the PIR cost per wallet action under load.

Requests to the proxy carry an x-bench-session header, so the proxy's own
events file also records the PIR internals for the replay.

Output: appends to <out> one line per request plus one "session" line per
(wallet, session) with its wall time.
"""

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from pathlib import Path


def load_trace(events: Path, pir_only: bool, exclude: set[str]):
    sessions: "OrderedDict[str, list]" = OrderedDict()
    for line in events.open():
        e = json.loads(line)
        if e.get("kind") != "rpc" or "params" not in e or not e.get("session"):
            continue
        if e["session"].startswith("replay:"):
            continue
        if e["method"] in exclude:
            continue
        if pir_only and e["route"] == "Fallback":
            continue
        sessions.setdefault(e["session"], []).append(e)
    for reqs in sessions.values():
        reqs.sort(key=lambda e: e["seq"])
    return sessions


def post(url: str, body: dict, header_session: str | None, timeout: float):
    data = json.dumps(body).encode()
    # Some public RPCs reject urllib's default User-Agent with 403.
    headers = {"content-type": "application/json", "user-agent": "pir-wallet-bench/0.1"}
    if header_session:
        headers["x-bench-session"] = header_session
    req = urllib.request.Request(url, data=data, headers=headers)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
        if not raw.lstrip().startswith(b"{"):
            raise
    dt = (time.perf_counter() - t0) * 1e3
    resp = json.loads(raw)
    return dt, "error" not in resp, len(data), len(raw)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--events", required=True, type=Path, help="events.jsonl from a drive run")
    ap.add_argument("--target", required=True, help="JSON-RPC URL")
    ap.add_argument("--label", required=True, help="e.g. pir or baseline")
    ap.add_argument("--concurrency", default="1,4,16")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--pir-only", action="store_true", help="replay only requests the proxy routed to PIR")
    ap.add_argument("--exclude", default="", help="comma list of methods to skip")
    ap.add_argument("--tag-sessions", action="store_true", help="send x-bench-session (use for the proxy target)")
    ap.add_argument("--timeout", type=float, default=120)
    args = ap.parse_args()

    # Never replay writes: a replay must not broadcast transactions again.
    never = {"eth_sendRawTransaction", "eth_sendTransaction", "eth_sendUserOperation"}
    sessions = load_trace(args.events, args.pir_only, never | {m for m in args.exclude.split(",") if m})
    n_req = sum(len(v) for v in sessions.values())
    if not n_req:
        print("no replayable requests (was the proxy run with --record-params?)", file=sys.stderr)
        return 2
    lock = threading.Lock()
    out = args.out.open("a")

    def emit(rec):
        with lock:
            out.write(json.dumps(rec) + "\n")
            out.flush()

    for level in [int(x) for x in args.concurrency.split(",") if x]:
        def wallet(w: int):
            for name, reqs in sessions.items():
                tag = f"replay:{args.label}:c{level}:w{w}:{name}" if args.tag_sessions else None
                t0 = time.perf_counter()
                errors = 0
                for i, e in enumerate(reqs):
                    body = {"jsonrpc": "2.0", "id": i, "method": e["method"], "params": e["params"]}
                    ts = time.time() * 1e3
                    try:
                        dt, ok, qb, rb = post(args.target, body, tag, args.timeout)
                        err = None
                    except Exception as x:  # network errors are data here
                        dt, ok, qb, rb, err = None, False, None, None, str(x)[:200]
                    errors += not ok
                    emit({"kind": "rpc", "label": args.label, "concurrency": level, "wallet": w,
                          "session": name, "ts": ts, "method": e["method"], "orig_route": e["route"],
                          "total_ms": dt, "ok": ok, "error": err, "req_bytes": qb, "resp_bytes": rb})
                emit({"kind": "session", "label": args.label, "concurrency": level, "wallet": w,
                      "session": name, "wall_ms": (time.perf_counter() - t0) * 1e3,
                      "requests": len(reqs), "errors": errors})

        t0 = time.perf_counter()
        threads = [threading.Thread(target=wallet, args=(w,)) for w in range(level)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wall = time.perf_counter() - t0
        emit({"kind": "level", "label": args.label, "concurrency": level, "wall_s": wall,
              "requests": n_req * level})
        print(f"{args.label:<9} c={level:<3} {n_req * level:>5} requests in {wall:7.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Merge the log of an upstream proxy under test into our recorded events.

Chain: kohaku-cli -> our proxy (--passthrough, records every request) -> the
upstream local-pir-rpc (decides PIR vs public RPC, logs one line per request).
Our events then hold params and end-to-end time; this script adds the
route the upstream proxy chose, its own elapsed time, and whether a PIR answer
came from its cache.

usage: merge_upstream.py <events.jsonl> <upstream.log>   (rewrites events in place)
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

LINE = re.compile(r'^(\S+Z)\s+INFO\s+\S*server: handled method="([^"]+)" route=(\w+).*?elapsed_ms=(\d+)')
# Format from local-pir-rpc de66713: "<method>  pir|fallback|error  <elapsed>  [<function>  <contract>]"
LINE2 = re.compile(r'^(\S+Z)\s+INFO\s+\S*server: (eth_\w+)\s+(pir|fallback|local|cache\w*)\s+([\d.]+)(ms|s)\b\s*(\S*)')
BATCH = re.compile(r'^(\S+Z)\s+INFO\s+\S*server: batch (\d+)\s+([\d.]+)(ms|s)')
ITEM = re.compile(r'^(\S+Z)\s+INFO\s+\S*server:\s{2,}(eth_\w+)\s+(pir|fallback|local|cache\w*)\s*(\S*)')
NAME_FNS = {"reverseResolve", "reverseWithGateways", "reverse", "name", "addr", "text", "resolve",
            "stealthMetaAddressOf"}


def pir_route(method, fn):
    """Route label for a PIR answer, from the method and the contract function in the log."""
    if fn == "balanceOf":
        return "TokenBalance"
    if fn in NAME_FNS:
        return "Names"
    return {"eth_getBalance": "AccountBalance", "eth_getTransactionCount": "AccountNonce",
            "eth_getCode": "AccountCode"}.get(method, "PIR")
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def ts_ms(stamp):
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp() * 1e3


def parse_log(path):
    out, batch_ms = [], 0
    for raw in Path(path).read_text(errors="replace").splitlines():
        line = ANSI.sub("", raw)
        m = LINE.match(line)
        if m:
            out.append({"end_ms": ts_ms(m.group(1)), "method": m.group(2), "route": m.group(3),
                        "ms": int(m.group(4)), "used": False})
            continue
        m = LINE2.match(line)
        if m:
            ms = float(m.group(4)) * (1000 if m.group(5) == "s" else 1)
            kind = m.group(3)
            route = {"fallback": "Fallback", "local": "Local"}.get(kind) or pir_route(m.group(2), m.group(6))
            out.append({"end_ms": ts_ms(m.group(1)), "method": m.group(2), "route": route, "ms": ms,
                        "used": False, "cache": kind.startswith("cache") or kind == "local"})
            continue
        m = BATCH.match(line)
        if m:
            batch_ms = float(m.group(3)) * (1000 if m.group(4) == "s" else 1)
            continue
        m = ITEM.match(line)
        if m:
            kind = m.group(3)
            route = {"fallback": "Fallback", "local": "Local"}.get(kind) or pir_route(m.group(2), m.group(4))
            out.append({"end_ms": ts_ms(m.group(1)), "method": m.group(2), "route": route, "ms": batch_ms,
                        "used": False, "cache": kind.startswith("cache") or kind == "local"})
    return out


def main():
    events_path, log_path = Path(sys.argv[1]), Path(sys.argv[2])
    lines = parse_log(log_path)
    events = [json.loads(l) for l in events_path.open() if l.strip()]
    matched = 0
    for e in events:
        if e.get("kind") != "rpc" or "upstream_route" in e:
            continue
        start, end = e["ts"], e["ts"] + e["total_ms"]
        # The upstream line is written when it finishes: inside our request window.
        for c in lines:
            if c["used"] or c["method"] != e["method"]:
                continue
            if start - 5 <= c["end_ms"] <= end + 50:
                c["used"] = True
                e["upstream_route"] = c["route"]
                e["route"] = c["route"]
                e["upstream_ms"] = c["ms"]
                e["cached"] = c.get("cache") or (c["route"] != "Fallback" and c["ms"] == 0)
                matched += 1
                break
    events_path.write_text("".join(json.dumps(e) + "\n" for e in events))
    rpc = sum(1 for e in events if e.get("kind") == "rpc")
    print(f"merged {matched} of {rpc} requests with {log_path.name}")


if __name__ == "__main__":
    main()

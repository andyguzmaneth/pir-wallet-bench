#!/usr/bin/env python3
"""Merge the log of an upstream proxy under test into our recorded events.

Chain: kohaku-cli -> our proxy (--passthrough, records every request) -> the
upstream local-pir-rpc (decides PIR vs public RPC, logs one "handled" line per
request). Our events then hold params and end-to-end time; this script adds the
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
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def parse_log(path):
    out = []
    for raw in Path(path).read_text(errors="replace").splitlines():
        m = LINE.match(ANSI.sub("", raw))
        if not m:
            continue
        ts = datetime.fromisoformat(m.group(1).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp() * 1e3
        out.append({"end_ms": ts, "method": m.group(2), "route": m.group(3), "ms": int(m.group(4)), "used": False})
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
                e["cached"] = c["route"] != "Fallback" and c["ms"] == 0
                matched += 1
                break
    events_path.write_text("".join(json.dumps(e) + "\n" for e in events))
    rpc = sum(1 for e in events if e.get("kind") == "rpc")
    print(f"merged {matched} of {rpc} requests with {log_path.name}")


if __name__ == "__main__":
    main()

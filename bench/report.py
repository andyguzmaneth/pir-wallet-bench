#!/usr/bin/env python3
"""Build a self-contained HTML report for one bench run directory.

Reads whatever the run produced (all optional except events.jsonl):
  events.jsonl         proxy events from the drive
  drive.jsonl          scenario wall times
  events-replay.jsonl  proxy events during the PIR replay (lock wait under load)
  replay.jsonl         replay results, labels "pir" and "baseline"
  pir-bench.jsonl      direct PIR load test
  meta.json            run config written by the Makefile
Writes <run>/report.html and <run>/summary.json.
"""

import html
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

C = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)", "var(--s5)", "var(--s6)"]


# ---------- data ----------

def read_jsonl(p: Path):
    if not p.exists():
        return []
    return [json.loads(l) for l in p.open() if l.strip()]


def pct(xs, q):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def fmt_ms(v):
    if v is None:
        return "–"
    return f"{v / 1000:.2f} s" if v >= 1000 else f"{v:.0f} ms"


def fmt_bytes(v):
    if v is None:
        return "–"
    for unit, div in (("MB", 1e6), ("KB", 1e3)):
        if v >= div:
            return f"{v / div:.1f} {unit}"
    return f"{v:.0f} B"


def esc(s):
    return html.escape(str(s), quote=True)


def is_pir(e):
    return e["route"] != "Fallback"


def fallback_category(e, mine=frozenset()):
    """Group a fallback request by what it asks for, and what it would take to serve it privately.
    `mine`: the wallet's own addresses (hex, no 0x)."""
    m = e["method"]
    call = e.get("call") or {}
    if m == "eth_getCode":
        a = str((e.get("params") or [""])[0]).lower().removeprefix("0x")
        if a in mine:
            return "eth_getCode · your EOA (7702 delegation check)", "your address"
        return "eth_getCode · contract", ""
    if m == "eth_call":
        kind = call.get("fn_kind") or call.get("to_kind") or "unknown"
        fn = (call.get("fn") or call.get("selector") or "?").split("(")[0]
        target = call.get("to_label") or call.get("to", "?")[:10] + "…"
        return f"eth_call · {kind} · {fn}", target
    if m == "eth_getLogs":
        labels = [l for l in (e.get("logs") or {}).get("labels", []) if l]
        return "eth_getLogs", ", ".join(labels) or "unlabeled"
    if m == "eth_sendRawTransaction":
        return "eth_sendRawTransaction · broadcast", ""
    if m in ("eth_getTransactionReceipt", "eth_getTransactionByHash"):
        return f"{m} · your transaction", ""
    if m in ("eth_estimateGas", "eth_fillTransaction"):
        return f"{m} · your transaction draft", ""
    if m in ("eth_getBalance", "eth_getTransactionCount"):
        return f"{m} (non-latest)", str(e.get("block_tag"))
    return m, ""


SERVABILITY = [
    ("eth_chainId", "static: answer locally, no network"),
    ("eth_blockNumber", "public head; no private input"),
    ("eth_gasPrice", "public; no private input"),
    ("eth_getLogs", "range query: not a PIR key lookup"),
    ("eth_getCode · your EOA", "account entry: delegate field (planned, same lookup cost)"),
    ("eth_sendRawTransaction", "becomes public on-chain anyway; hide who sent it with network anonymity (anon-RPC), not PIR"),
    ("eth_getTransactionReceipt", "a receipts-by-hash PIR table, or poll over anon-RPC"),
    ("eth_getTransactionByHash", "a transactions-by-hash PIR table, or poll over anon-RPC"),
    ("eth_estimateGas", "simulation needs execution, not a lookup: estimate locally or over anon-RPC"),
    ("eth_fillTransaction", "fill nonce, fees and gas locally (nonce from PIR) instead of asking the RPC"),
    ("eth_maxPriorityFeePerGas", "public fee data; no private input"),
    ("eth_getBlockByNumber", "public block data; no private input"),
    ("eth_getCode · contract", "public contract code; no private input"),
    ("eth_call · erc20 · balanceOf", "erc20-balances tier (pir-state-pipeline)"),
    ("eth_call · erc20", "token metadata: public, cacheable"),
    ("eth_call · ens", "ens-records tier"),
    ("eth_call · names", "name-records tier (GNS / WNS reverse lookups)"),
    ("eth_call · stealth", "needs OMR (oblivious message retrieval), not PIR"),
    ("eth_call · privacy", "privacy-protocol-state tier"),
    ("eth_call · oracle", "public price data; cacheable"),
    ("eth_call · dex", "public pool data; cacheable"),
    ("eth_call · multicall", "decode inner calls (see table)"),
]


# One-line descriptions of request groups, matched by prefix (first match wins).
DESCRIPTIONS = [
    ("eth_call · erc20 · balanceOf", "Token balance (USDC, USDT, DAI, ...) of one of your addresses. Reveals your "
                                     "address and what you hold."),
    ("eth_call · erc20", "Token metadata (decimals, symbol, name). Shows which tokens you look at."),
    ("eth_chainId", "Asks which chain the RPC serves (always mainnet). kohaku-cli's safety check, repeated for each "
                    "RPC client it creates."),
    ("eth_blockNumber", "Current chain head, used to know how far to sync. Same for everyone."),
    ("eth_sendRawTransaction", "Broadcasts your signed transaction. It becomes public on-chain, but the RPC also learns "
                               "the IP that sent it."),
    ("eth_getTransactionReceipt", "Polls for your transaction's receipt after sending. Shows the RPC which transaction "
                                  "is yours."),
    ("eth_getTransactionByHash", "Looks up your just-sent transaction. Shows the RPC which transaction is yours."),
    ("eth_estimateGas", "Simulates your transaction (from, to, value) to price gas. Reveals the draft before you send."),
    ("eth_fillTransaction", "Asks the RPC to fill nonce, fees and gas for your transaction draft. Reveals the draft."),
    ("eth_maxPriorityFeePerGas", "Current tip suggestion. Same for everyone."),
    ("eth_getBlockByNumber", "Reads a block (fee data or chain head). Same for everyone."),
    ("eth_getCode · your EOA", "Reads the code at your own address to check for an EIP-7702 delegation. Reveals "
                               "your address. Planned: the delegate address rides in the PIR account entry."),
    ("eth_getCode", "Fetches a public contract's code to check it exists."),
    ("eth_getLogs", "Reads all events of a public contract over a block range (stealth announcements, Privacy Pools), "
                    "not just yours."),
    ("eth_call · privacy · denomination", "A Tornado pool's fixed deposit size (for example 0.1 ETH). A public "
                                          "constant."),
    ("eth_call · privacy · ROOT_HISTORY_SIZE", "How many recent Merkle roots a Tornado pool keeps. A public constant."),
    ("eth_call · privacy · instances", "A Tornado pool's settings from the instance registry (token, size, state). "
                                       "Public."),
    ("eth_call · privacy · getAllInstanceAddresses", "The list of all Tornado pools from the registry. Public."),
    ("eth_call · privacy · isSpent", "Whether a specific note was already withdrawn. Reveals which note is yours."),
    ("eth_call · privacy · isKnownRoot", "Whether a Merkle root is recent. Can link a withdrawal to its proof."),
    ("eth_call · privacy", "A view call on a privacy-protocol contract with no address of yours in it."),
    ("eth_call · names · reverseResolve", "GNS / WNS reverse lookup: which name belongs to your address. Reveals "
                                          "your address."),
    ("eth_call · ens · reverseWithGateways", "ENS reverse lookup (primary name) of your address through the "
                                             "universal resolver. Reveals your address."),
    ("eth_call · ens", "An ENS record lookup."),
    ("eth_call · stealth · stealthMetaAddressOf", "Your stealth meta-address in the ERC-6538 registry. Carries your "
                                                  "address; gray by decision, because private stealth reads need OMR."),
    ("eth_call · dex", "A DEX pool, pair or price query. Shows which pair you care about."),
    ("eth_call · oracle", "A price-feed read. Shows which asset you care about."),
    ("eth_call · multicall", "A batch of contract calls in one request; see the inner calls."),
]


def describe(cat):
    for prefix, text in DESCRIPTIONS:
        if cat.startswith(prefix):
            return text
    return "No description yet: an unidentified call."


def servability(cat):
    for prefix, note in SERVABILITY:
        if cat.startswith(prefix):
            return note
    return ""


# ---------- svg ----------

def nice_ticks(vmax, n=5):
    if vmax <= 0:
        return [0, 1]
    raw = vmax / n
    mag = 10 ** math.floor(math.log10(raw))
    step = min((s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw), default=mag * 10)
    return [i * step for i in range(int(math.ceil(vmax / step)) + 1)]


def legend(items, kind="rect"):
    parts = []
    for label, color in items:
        key = (f'<span class="key rect" style="background:{color}"></span>' if kind == "rect"
               else f'<span class="key line" style="background:{color}"></span>')
        parts.append(f'<span class="lg">{key}{esc(label)}</span>')
    return f'<div class="legend">{"".join(parts)}</div>'


def stacked_hbar(rows, series, unit_fmt, width=720, colors=None):
    """rows: [(label, [v per series], tip_extra)]; horizontal stacked bars from one baseline."""
    colors = colors or C
    longest = max((len(str(r[0])) for r in rows), default=10)
    bar_h, gap, right, top = 20, 14, 70, 8
    left = min(max(120, 16 + longest * 7), 330)
    total_max = max((sum(v) for _, v, _ in rows), default=0) or 1
    ticks = nice_ticks(total_max)
    xmax = ticks[-1]
    plot_w = width - left - right
    h = top + len(rows) * (bar_h + gap) + 24
    sx = lambda v: left + v / xmax * plot_w
    out = [f'<svg viewBox="0 0 {width} {h}" class="chart" role="img">']
    for t in ticks:
        x = sx(t)
        out.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{top}" y2="{h - 22}" class="grid"/>')
        out.append(f'<text x="{x:.1f}" y="{h - 6}" class="tick" text-anchor="middle">{esc(unit_fmt(t))}</text>')
    for i, (label, vals, extra) in enumerate(rows):
        y = top + i * (bar_h + gap)
        out.append(f'<text x="{left - 10}" y="{y + bar_h / 2 + 4}" class="lab" text-anchor="end">{esc(label)}</text>')
        x = left
        nonzero = [j for j, v in enumerate(vals) if v > 0]
        for j, v in enumerate(vals):
            if v <= 0:
                continue
            w = v / xmax * plot_w
            is_last = j == nonzero[-1]
            w_draw = max(w - (0 if is_last else 2), 0.5)
            tip = f"{label} · {series[j]}: {unit_fmt(v)}" + (f" · {extra}" if extra else "")
            if is_last and w_draw > 4:
                out.append(f'<path d="M{x:.1f},{y} h{w_draw - 4:.1f} a4,4 0 0 1 4,4 v{bar_h - 8} a4,4 0 0 1 -4,4 h{-(w_draw - 4):.1f} z" '
                           f'fill="{colors[j]}" class="mark" data-tip="{esc(tip)}" tabindex="0"/>')
            else:
                out.append(f'<rect x="{x:.1f}" y="{y}" width="{w_draw:.1f}" height="{bar_h}" fill="{colors[j]}" '
                           f'class="mark" data-tip="{esc(tip)}" tabindex="0"/>')
            x += w
        out.append(f'<text x="{x + 6:.1f}" y="{y + bar_h / 2 + 4}" class="val">{esc(unit_fmt(sum(vals)))}</text>')
    out.append(f'<line x1="{left}" x2="{left}" y1="{top - 4}" y2="{h - 22}" class="axis"/>')
    out.append("</svg>")
    return "".join(out)


def grouped_hbar(groups, series, unit_fmt, width=720, colors=None):
    """groups: [(label, [value or None per series], [tip per series])]; bars side by side from one baseline."""
    longest = max((len(str(g[0])) for g in groups), default=10)
    bar_h, inner, gap, right, top = 14, 3, 18, 80, 8
    left = min(max(120, 16 + longest * 7), 330)
    ns = len(series)
    colors = colors or C
    vmax = max((v for g in groups for v in g[1] if v), default=0) or 1
    ticks = nice_ticks(vmax)
    xmax = ticks[-1]
    plot_w = width - left - right
    gh = ns * bar_h + (ns - 1) * inner
    h = top + len(groups) * (gh + gap) + 24
    out = [f'<svg viewBox="0 0 {width} {h}" class="chart" role="img">']
    for t in ticks:
        x = left + t / xmax * plot_w
        out.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{top}" y2="{h - 22}" class="grid"/>')
        out.append(f'<text x="{x:.1f}" y="{h - 6}" class="tick" text-anchor="middle">{esc(unit_fmt(t))}</text>')
    for gi, g in enumerate(groups):
        label, vals, tips = g[:3]
        bar_colors = g[3] if len(g) > 3 else [None] * ns
        y0 = top + gi * (gh + gap)
        out.append(f'<text x="{left - 10}" y="{y0 + gh / 2 + 4}" class="lab" text-anchor="end">{esc(label)}</text>')
        for j, v in enumerate(vals):
            y = y0 + j * (bar_h + inner)
            if not v:
                out.append(f'<text x="{left + 6}" y="{y + bar_h - 3}" class="tick">none</text>')
                continue
            w = max(v / xmax * plot_w, 1)
            tip = f"{label} · {series[j]}: {unit_fmt(v)}" + (f" · {tips[j]}" if tips and tips[j] else "")
            if w > 4:
                out.append(f'<path d="M{left},{y} h{w - 4:.1f} a4,4 0 0 1 4,4 v{bar_h - 8} a4,4 0 0 1 -4,4 h{-(w - 4):.1f} z" '
                           f'fill="{bar_colors[j] or colors[j]}" class="mark" data-tip="{esc(tip)}" tabindex="0"/>')
            else:
                out.append(f'<rect x="{left}" y="{y}" width="{w:.1f}" height="{bar_h}" fill="{bar_colors[j] or colors[j]}" class="mark" data-tip="{esc(tip)}" tabindex="0"/>')
            out.append(f'<text x="{left + w + 6:.1f}" y="{y + bar_h - 3}" class="val">{esc(unit_fmt(v))}</text>')
    out.append(f'<line x1="{left}" x2="{left}" y1="{top - 4}" y2="{h - 22}" class="axis"/>')
    out.append("</svg>")
    return "".join(out)


def ecdf_chart(series, width=720, height=280):
    """series: [(label, [ms...])]; log-x cumulative distribution with crosshair."""
    left, right, top, bottom = 48, 36, 10, 30
    allv = [v for _, xs in series for v in xs if v and v > 0]
    if not allv:
        return "<p class='muted'>No data.</p>"
    lo = 10 ** math.floor(math.log10(min(allv)))
    hi = 10 ** math.ceil(math.log10(max(allv)))
    pw, ph = width - left - right, height - top - bottom
    sx = lambda v: left + (math.log10(v) - math.log10(lo)) / (math.log10(hi) - math.log10(lo)) * pw
    sy = lambda p: top + (1 - p) * ph
    out = [f'<svg viewBox="0 0 {width} {height}" class="chart ecdf" role="img" data-left="{left}" data-right="{width - right}">']
    for p in (0, 0.25, 0.5, 0.75, 1):
        out.append(f'<line x1="{left}" x2="{width - right}" y1="{sy(p):.1f}" y2="{sy(p):.1f}" class="grid"/>')
        out.append(f'<text x="{left - 8}" y="{sy(p) + 4:.1f}" class="tick" text-anchor="end">{int(p * 100)}%</text>')
    d = lo
    while d <= hi * 1.0001:
        out.append(f'<line x1="{sx(d):.1f}" x2="{sx(d):.1f}" y1="{top}" y2="{top + ph}" class="grid"/>')
        out.append(f'<text x="{sx(d):.1f}" y="{height - 10}" class="tick" text-anchor="middle">{esc(fmt_ms(d))}</text>')
        d *= 10
    for i, (label, xs) in enumerate(series):
        xs = sorted(v for v in xs if v and v > 0)
        if not xs:
            continue
        pts = []
        n = len(xs)
        for k, v in enumerate(xs):
            pts.append(f"{sx(v):.1f},{sy(k / n):.1f}")
            pts.append(f"{sx(v):.1f},{sy((k + 1) / n):.1f}")
        out.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="{C[i]}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
        med = pct(xs, 0.5)
        out.append(f'<circle cx="{sx(med):.1f}" cy="{sy(0.5):.1f}" r="4" fill="{C[i]}" class="ring"/>')
        out.append(f'<text x="{sx(med) + 8:.1f}" y="{sy(0.5) - 8:.1f}" class="lab">{esc(label)} p50 {esc(fmt_ms(med))}</text>')
        data = json.dumps([[round(sx(v), 1), round(v, 1)] for v in xs])
        out.append(f'<g class="ecdf-data" data-label="{esc(label)}" data-color="{C[i]}" data-pts=\'{esc(data)}\'></g>')
    out.append(f'<line x1="{left}" x2="{width - right}" y1="{top + ph}" y2="{top + ph}" class="axis"/>')
    out.append(f'<line class="xhair" x1="0" x2="0" y1="{top}" y2="{top + ph}" visibility="hidden"/>')
    out.append(f'<rect class="hit" x="{left}" y="{top}" width="{pw}" height="{ph}" fill="transparent"/>')
    out.append("</svg>")
    return "".join(out)


def line_chart(series, x_label, y_fmt, width=720, height=240, x_fmt=str):
    """series: [(label, [(x, y)...])] with a shared numeric x."""
    left, right, top, bottom = 64, 110, 10, 34
    xs = [x for _, pts in series for x, _ in pts]
    ys = [y for _, pts in series for _, y in pts if y is not None]
    if not xs or not ys:
        return "<p class='muted'>No data.</p>"
    x0, x1 = min(xs), max(xs)
    if x1 == x0:
        x1 = x0 + 1
    ticks = nice_ticks(max(ys))
    ymax = ticks[-1]
    pw, ph = width - left - right, height - top - bottom
    sx = lambda x: left + (x - x0) / (x1 - x0) * pw
    sy = lambda y: top + (1 - y / ymax) * ph
    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img">']
    ends = []
    for t in ticks:
        out.append(f'<line x1="{left}" x2="{width - right}" y1="{sy(t):.1f}" y2="{sy(t):.1f}" class="grid"/>')
        out.append(f'<text x="{left - 8}" y="{sy(t) + 4:.1f}" class="tick" text-anchor="end">{esc(y_fmt(t))}</text>')
    for x in sorted(set(xs)) if len(set(xs)) <= 12 else nice_ticks(x1 - x0, 6):
        xv = x if len(set(xs)) <= 12 else x0 + x
        if xv > x1:
            continue
        out.append(f'<text x="{sx(xv):.1f}" y="{height - 16}" class="tick" text-anchor="middle">{esc(x_fmt(xv))}</text>')
    out.append(f'<text x="{left + pw / 2:.1f}" y="{height - 2}" class="tick" text-anchor="middle">{esc(x_label)}</text>')
    for i, (label, pts) in enumerate(series):
        pts = [(x, y) for x, y in pts if y is not None]
        if not pts:
            continue
        poly = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in pts)
        out.append(f'<polyline points="{poly}" fill="none" stroke="{C[i]}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
        for x, y in pts:
            tip = f"{label}: {y_fmt(y)} at {x_label} {x_fmt(x)}"
            out.append(f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="4" fill="{C[i]}" class="ring"/>'
                       f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="12" fill="transparent" class="mark" data-tip="{esc(tip)}" tabindex="0"/>')
        lx, ly = pts[-1]
        ends.append((sy(ly), sx(lx), label))
    # End labels only when they separate; otherwise the legend and tooltips carry identity.
    ys_end = sorted(y for y, _, _ in ends)
    if all(b - a >= 14 for a, b in zip(ys_end, ys_end[1:])):
        for y, x, label in ends:
            out.append(f'<text x="{x + 10:.1f}" y="{y + 4:.1f}" class="lab">{esc(label)}</text>')
    out.append(f'<line x1="{left}" x2="{width - right}" y1="{top + ph}" y2="{top + ph}" class="axis"/>')
    out.append("</svg>")
    return "".join(out)


class Raw(str):
    """Table cell holding trusted HTML (built by this script, values escaped)."""


def cell(c):
    return c if isinstance(c, Raw) else esc(c)


def table(headers, rows, num_from=1, text_cols=()):
    isnum = lambda i: i >= num_from and i not in text_cols
    th = "".join(f'<th class="{"num" if isnum(i) else ""}">{esc(h)}</th>' for i, h in enumerate(headers))
    body = "".join(
        "<tr>" + "".join(f'<td class="{"num" if isnum(i) else ""}">{cell(c)}</td>' for i, c in enumerate(r)) + "</tr>"
        for r in rows)
    return f'<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>'


def details_table(headers, rows, num_from=1, summary="Table view", text_cols=()):
    return f"<details><summary>{esc(summary)}</summary>{table(headers, rows, num_from, text_cols)}</details>"


def insight(title, value, detail, anchor, todo=""):
    return (f'<a class="card" href="#{anchor}"><div class="ct">{esc(title)}</div><div class="cv">{esc(value)}</div>'
            f'<div class="cd">{esc(detail)}</div>'
            + (f'<div class="todo"><b>What we can do</b><button type="button" class="info" '
               f'aria-label="What we can do" data-title="What we can do" data-tip="{esc(todo)}">i</button></div>'
               if todo else "") + '</a>')


METHOD = (
    "<p>kohaku-cli runs scripted sessions on a throwaway, unfunded mainnet wallet through Kohaku's "
    "<code>local-pir-rpc</code> proxy. The proxy sends latest-block ETH balance and nonce lookups to the PIR server "
    "and everything else to a public RPC. Three local patches change the vanilla setup: the PIR client (inspire-gpu-serving) times "
    "each phase of a lookup and counts bytes on the wire; the Kohaku router passes those numbers up through a traced "
    "request path; the proxy writes one JSON line per request with session, method, route, latency, lock wait, "
    "payload sizes, target contract and function, log ranges, chain head, PIR snapshot block and raw params.</p>"
    "<p>From those lines the report measures served share, latency per route, the phases and bytes of each PIR "
    "lookup, snapshot lag, what the public-RPC traffic asks for, and which public-RPC requests carry addresses PIR "
    "hid. The recorded sessions are then replayed at 1, 4 and 16 parallel wallets through the proxy and direct to the "
    "public RPC, and pir-bench loads the PIR server directly with independent clients, which separates server "
    "capacity from the proxy's queueing.</p>")


def tile(label, value, sub=""):
    return (f'<div class="tile"><div class="tl">{esc(label)}</div><div class="tv">{esc(value)}</div>'
            f'<div class="ts">{esc(sub)}</div></div>')


# ---------- report ----------

# Requests left out of every number in the report (none today). Requests with no
# private input are kept and marked privacy-insensitive instead; see sensitivity().
EXCLUDED_METHODS: set = set()

# Contract calls that reveal a specific note or deposit even without an address.
SENSITIVE_FNS = {"isSpent", "nullifierHashes", "isKnownRoot"}
# Calls that reveal intent: which tokens or trading pairs the user cares about.
INTENT_KINDS = {"dex", "oracle"}
INTENT_FNS = {"decimals", "symbol", "name", "totalSupply"}
# Treated as privacy-insensitive by decision: stealth-address privacy needs
# oblivious message retrieval (OMR), not PIR; today clients trial-decrypt every
# announcement anyway.
INSENSITIVE_KINDS = {"stealth"}


def build(run: Path):
    raw = [e for e in read_jsonl(run / "events.jsonl")
           if e.get("kind") == "rpc" and not e.get("session", "").startswith("replay:")]
    excluded = [e for e in raw if e["method"] in EXCLUDED_METHODS]
    events = [e for e in raw if e["method"] not in EXCLUDED_METHODS]
    drive = read_jsonl(run / "drive.jsonl")
    replay_all = read_jsonl(run / "replay.jsonl")
    # Replayed sessions ran their requests one after another, so removing the
    # excluded requests means subtracting their time from the session wall time.
    drop = defaultdict(float)
    for r in replay_all:
        if r.get("kind") == "rpc" and r["method"] in EXCLUDED_METHODS:
            drop[(r["label"], r["concurrency"], r["wallet"], r["session"])] += r["total_ms"] or 0
    replay = []
    for r in replay_all:
        if r.get("kind") == "rpc" and r["method"] in EXCLUDED_METHODS:
            continue
        if r.get("kind") == "session":
            r = dict(r, wall_ms=r["wall_ms"] - drop.get((r["label"], r["concurrency"], r["wallet"], r["session"]), 0))
        replay.append(r)
    replay_ev = [e for e in read_jsonl(run / "events-replay.jsonl")
                 if e.get("kind") == "rpc" and e["method"] not in EXCLUDED_METHODS]
    excluded_note = ""
    pbench = read_jsonl(run / "pir-bench.jsonl")
    meta = json.loads((run / "meta.json").read_text()) if (run / "meta.json").exists() else {}
    if not events:
        sys.exit(f"no rpc events in {run / 'events.jsonl'}")

    pir = [e for e in events if is_pir(e)]
    fb = [e for e in events if not is_pir(e)]
    pir_ms = [e["total_ms"] for e in pir]
    fb_ms = [e["total_ms"] for e in fb]
    t_pir, t_fb = sum(pir_ms), sum(fb_ms)
    heads = [e["head_block"] for e in events if "head_block" in e]
    snaps = [e["pir"]["snapshot_block"] for e in pir if e.get("pir", {}).get("snapshot_block")]
    lags = []
    for e in pir:
        s = e.get("pir", {}).get("snapshot_block")
        prior = [h for h in ((x["ts"], x["head_block"]) for x in events if "head_block" in x) if h[0] <= e["ts"]]
        if s and prior:
            lags.append(prior[-1][1] - s)
    # Addresses PIR looked up privately, and fallback requests that still carry them in plaintext.
    hidden = {e["account"]["address"][2:] for e in pir if e.get("account")}
    def exposes(e):
        blob = json.dumps(e.get("params", "")).lower()
        return bool(hidden) and any(a in blob for a in hidden)
    exposing = [e for e in fb if exposes(e)]

    def why_sensitive(e):
        """Why a request is privacy-sensitive, or "" if it carries nothing about the user."""
        if e["method"] in ("eth_getTransactionReceipt", "eth_getTransactionByHash"):
            return "your transaction (links you to it)"
        if e["method"] == "eth_sendRawTransaction":
            return "your transaction (broadcast)"
        call = e.get("call") or {}
        kind = call.get("fn_kind") or call.get("to_kind") or ""
        fn = (call.get("fn") or "").split("(")[0]
        if kind in INSENSITIVE_KINDS:
            return ""
        if is_pir(e) or exposes(e):
            return "your address"
        if fn in SENSITIVE_FNS:
            return "a specific note"
        if kind in INTENT_KINDS or (kind == "erc20" and fn in INTENT_FNS):
            return "intent: tokens or pairs you care about"
        return ""

    def sensitive(e):
        return bool(why_sensitive(e))
    sens = [e for e in events if sensitive(e)]
    insens = [e for e in events if not sensitive(e)]
    fb_sens = [e for e in fb if sensitive(e)]
    wire_up = [e["pir"]["req_bytes"] for e in pir if "pir" in e]
    wire_dn = [e["pir"]["resp_bytes"] for e in pir if "pir" in e]
    rpc_up = [e["rpc_req_bytes"] for e in pir]
    rpc_dn = [e["rpc_resp_bytes"] for e in pir]

    summary = {
        "requests": len(events), "pir_requests": len(pir), "fallback_requests": len(fb),
        "pir_share_requests": len(pir) / len(events),
        "pir_share_time": t_pir / (t_pir + t_fb) if t_pir + t_fb else None,
        "pir_p50_ms": pct(pir_ms, .5), "pir_p95_ms": pct(pir_ms, .95),
        "fallback_p50_ms": pct(fb_ms, .5), "fallback_p95_ms": pct(fb_ms, .95),
        "pir_req_bytes_p50": pct(wire_up, .5), "pir_resp_bytes_p50": pct(wire_dn, .5),
        "rpc_req_bytes_p50_same_requests": pct(rpc_up, .5), "rpc_resp_bytes_p50_same_requests": pct(rpc_dn, .5),
        "snapshot_lag_blocks_p50": pct(lags, .5),
        "pir_hidden_addresses": len(hidden),
        "fallback_requests_carrying_hidden_address": len(exposing),
    }

    order = [d["scenario"] for d in drive] or sorted({e["session"] for e in events})
    by_s = defaultdict(list)
    for e in events:
        by_s[e["session"]].append(e)

    # Same request type, two routes: PIR (drive, through the proxy) vs public RPC
    # (baseline replay at the lowest load level sends the identical request direct).
    rrpc = [r for r in replay if r.get("kind") == "rpc"]
    base_c = min((r["concurrency"] for r in rrpc if r["label"] == "baseline"), default=None)
    base = [r for r in rrpc if r["label"] == "baseline" and r["concurrency"] == base_c and r["ok"]]
    pir_methods = sorted({e["method"] for e in pir})
    lat_rows, lat_bars = [], []
    for m in pir_methods:
        a = [e["total_ms"] for e in pir if e["method"] == m]
        b = [r["total_ms"] for r in base if r["method"] == m]
        if not b:  # no replay yet: public RPC latency of the other requests is the reference
            b = fb_ms
        lat_rows.append([m, len(a), fmt_ms(pct(a, .5)), fmt_ms(pct(a, .95)), len(b), fmt_ms(pct(b, .5)),
                         fmt_ms(pct(b, .95)), f"{pct(a, .5) / pct(b, .5):.0f}×" if b else "–"])
        lat_bars += [(f"{m} · PIR", [pct(a, .5), 0], f"p95 {fmt_ms(pct(a, .95))}, n={len(a)}"),
                     (f"{m} · public RPC", [0, pct(b, .5)], f"p95 {fmt_ms(pct(b, .95))}, n={len(b)}")]
    bal_pir = pct([e["total_ms"] for e in pir if e["method"] == "eth_getBalance"], .5)
    bal_pub = pct([r["total_ms"] for r in base if r["method"] == "eth_getBalance"], .5) or pct(fb_ms, .5)

    # Replay: whole wallet actions
    sess = [r for r in replay if r.get("kind") == "session"]
    NAMES = {"pir": "PIR proxy", "baseline": "Public RPC only"}
    ratios = []
    if sess:
        c_lo = min(r["concurrency"] for r in sess)
        for sname in order:
            v = {l: pct([r["wall_ms"] for r in sess if r["label"] == l and r["concurrency"] == c_lo and r["session"] == sname], .5)
                 for l in ("pir", "baseline")}
            if v["pir"] and v["baseline"] and "balances" in sname:
                ratios.append((sname, v["pir"], v["baseline"]))

    # Fallback demand
    cats = defaultdict(lambda: {"n": 0, "ms": 0.0, "targets": Counter(), "exp": 0, "sens": 0, "why": Counter()})
    for e in fb:
        cat, target = fallback_category(e, hidden)
        c = cats[cat]
        c["n"] += 1
        c["exp"] += exposes(e)
        c["sens"] += sensitive(e)
        if why_sensitive(e):
            c["why"][why_sensitive(e)] += 1
        c["ms"] += e["total_ms"]
        if target:
            c["targets"][target] += 1
    ranked = sorted(cats.items(), key=lambda kv: -kv[1]["n"])

    # Load
    looks = [r for r in pbench if r.get("kind") == "lookup" and r.get("ok")]
    load_levels = sorted({r["concurrency"] for r in looks})
    by_c = {c: [r["total_ms"] for r in looks if r["concurrency"] == c] for c in load_levels}
    thr = {r["concurrency"]: r for r in pbench if r.get("kind") == "level"}
    hi = max(load_levels) if load_levels else None
    proxy_hi = None
    if sess:
        c_hi = max(r["concurrency"] for r in sess)
        lock_hi = [e["pir"]["lock_wait_ms"] for e in replay_ev
                   if "pir" in e and e.get("session", "").startswith(f"replay:pir:c{c_hi}:")]
        proxy_hi = (c_hi, pct([r["wall_ms"] for r in sess if r["label"] == "pir" and r["concurrency"] == c_hi], .5),
                    pct([r["wall_ms"] for r in sess if r["label"] == "pir" and r["concurrency"] == c_lo], .5),
                    pct(lock_hi, .5))

    # Lookup breakdown from the probe pairs (pir-bench --probes): each pair is a
    # warm-connection upload probe (round trip + upload, no GPU work), a TCP
    # connect (one round trip), and a real lookup taken right after.
    probes = [r for r in pbench if r.get("kind") == "probe" and r.get("lookup_ok") and r.get("connect_ms")]
    steps = None
    if probes:
        rtt = pct([r["connect_ms"] for r in probes], .5)
        up_rtt = pct([r["upload_rtt_ms"] for r in probes], .5)
        http = pct([r["http_ms"] for r in probes], .5)
        steps = [
            ("Build query (client)", pct([r["query_build_ms"] for r in probes], .5), "client CPU"),
            ("Network round trip", rtt, "TCP connect time"),
            ("Upload", max(up_rtt - rtt, 0), f"{fmt_bytes(pct([r['req_bytes'] for r in probes], .5))}"),
            ("Server compute", max(http - up_rtt, 0), "PIR answer on the GPU, plus the front hop"),
            ("Download", pct([r["body_ms"] for r in probes], .5), f"{fmt_bytes(pct([r['resp_bytes'] for r in probes], .5))}"),
            ("Decode (client)", pct([r["decode_ms"] for r in probes], .5), "client CPU"),
        ]
        steps_total = sum(v for _, v, _ in steps)
        up_bps = pct([r["req_bytes"] for r in probes], .5) / (max(up_rtt - rtt, 1) / 1e3)
        dn_bps = pct([r["resp_bytes"] for r in probes], .5) / (max(pct([r["body_ms"] for r in probes], .5), 1) / 1e3)
        side_share = pct([r["sidecar_bytes"] / r["resp_bytes"] for r in probes if r["resp_bytes"]], .5)

    nav = [("summary", "Summary"), ("requests", "Request time"), ("latency", "Per-request latency"),
           ("wallet", "Wallet actions"), ("lookup", "Inside a lookup"), ("load", "Under load"), ("run", "Method & run")]
    parts = []

    # --- summary
    n_bal = [len([e for e in by_s.get(x, []) if is_pir(e)]) for x in order if "balances" in x]
    n_all = [len(by_s.get(x, [])) for x in order if "balances" in x]
    tcp = json.loads((run / "tcp-experiment.json").read_text()) if (run / "tcp-experiment.json").exists() else None
    tcp_txt = ""
    if tcp:
        r0, r2 = tcp["rows"][0], tcp["rows"][-1]
        tcp_txt = (f" Measured here: reusing one connection and turning off TCP's idle restart cut a lookup from "
                   f"{fmt_ms(r0['lookup_ms'])} to {fmt_ms(r2['lookup_ms'])}.")
    cards = [
        insight("Share of privacy-sensitive requests that go to PIR", f"{len(pir) / len(sens):.0%}",
                f"{len(pir)} of {len(sens)} requests that carry one of your addresses, a specific note, or your intent (tokens or pairs you look up). PIR holds latest-block "
                f"ETH balance and nonce, so that is all it can answer today; the other {len(fb_sens)} still go to a "
                f"public RPC. Not counted: {len(insens)} requests with no private input (chain id, block number, "
                "public protocol state), shown in gray.", "requests",
                "Actively adding: ERC-20 balances and name records (ENS / GNS / WNS). Planned: the EIP-7702 delegate "
                "address inside the account entry, so balance, nonce and the eth_getCode delegation check come from "
                "one lookup at today's cost."),
        insight("One PIR lookup vs the same request over a public RPC", f"{fmt_ms(bal_pir)} vs {fmt_ms(bal_pub)}",
                f"eth_getBalance, median: {bal_pir / bal_pub:.0f}× slower. A lookup is one request for one address."
                if bal_pir and bal_pub else "", "latency",
                "Most of the gap is network handling, not cryptography (next card), so it can shrink without "
                "changing the PIR scheme."),
    ]
    if steps:
        d_ = dict((n, v) for n, v, _ in steps)
        move = d_["Upload"] + d_["Download"] + d_["Network round trip"]
        cards.append(insight("Why a lookup takes so long: the network, not the GPU",
                             f"{move / steps_total:.0%} is moving bytes",
                             f"Upload {fmt_ms(d_['Upload'])}, download {fmt_ms(d_['Download'])}, round trip "
                             f"{fmt_ms(d_['Network round trip'])}; server compute only {fmt_ms(d_['Server compute'])}. "
                             "The link is fast (about 85 Mbit/s each way here), but every lookup opens a new connection "
                             "on a 150 ms path, so TCP restarts its slow ramp-up each time.", "lookup",
                             "Send lookups as one batch per wallet refresh: 8 lookups in one request clear slow start "
                             "in about 9 round trips instead of 48. Plain connection reuse helps without anon-RPC but "
                             "links lookups that share a connection or Tor circuit, so the batch is the unit to optimize. "
                             "Planned on the server: TCP idle restart off and BBR." + tcp_txt))
    if wire_up:
        cards.append(insight("Data cost per lookup", f"~{fmt_bytes(pct(wire_up, .5) + pct(wire_dn, .5))}",
                             f"Up {fmt_bytes(pct(wire_up, .5))}: two encrypted queries of fixed size, the same for any "
                             f"address, so the server cannot tell which one was asked. Down {fmt_bytes(pct(wire_dn, .5))}: "
                             "two encrypted answers plus the sidecar (every account change since the last database "
                             f"rebuild, sent to every client). A public-RPC request is {fmt_bytes(pct(rpc_up, .5))} up, "
                             f"{fmt_bytes(pct(rpc_dn, .5))} down.", "lookup",
                             "Planned: gzip and a binary sidecar, ~211 KB to ~65 KB down (~970 KB to ~800 KB per "
                             "lookup). A fixed-size padded batch sends the sidecar once per batch, but padding adds "
                             "upload for wallets with few addresses. Shrinking the encrypted queries (one instead of "
                             "two) needs research or changes to the architecture."))
    if ratios:
        rs = sorted(r[1] / r[2] for r in ratios)
        cards.append(insight("A whole wallet action (kohaku balances) with vs without PIR",
                             f"{rs[0]:.1f}×–{rs[-1]:.1f}× longer",
                             f"One balances command is {min(n_all)}–{max(n_all)} requests, of which {max(n_bal)} are PIR "
                             "lookups, one per wallet address, run one after another. Replay at 1 wallet.", "wallet",
                             "A cache in the middleware: one lookup per address per block answers balance, nonce and "
                             "the delegation check. With one padded batch per refresh for all wallet addresses, a "
                             "command waits about one batch instead of one lookup per address."))
    if hi and proxy_hi:
        cards.append(insight("The PIR server handles parallel wallets; the proxy does not",
                             f"{fmt_ms(pct(by_c[hi], .5))} vs {fmt_ms(proxy_hi[3])}",
                             f"Lookup median with {hi} parallel clients hitting the server directly, vs the median wait "
                             f"for the proxy's single PIR client at {proxy_hi[0]} wallets.", "load",
                             "A pool of PIR clients removes the queue (PR open: 6.7 s to 1.0 s per lookup at 4 "
                             "wallets, 28 s to 1.2 s at 16). If the proxy moves into Kohaku, it needs the wallet's "
                             "address list up front to send one padded batch per refresh."))
    if ranked:
        k, v = max(((k, v) for k, v in ranked if v.get("sens")), key=lambda kv: kv[1]["sens"], default=ranked[0])
        cards.append(insight("Largest privacy-sensitive request type PIR does not hold yet",
                             k.replace("eth_call · ", "").replace(" · ", " "),
                             f"{v['n']} requests, {v['n'] / len(fb):.0%} of public-RPC traffic. "
                             f"Route: {servability(k) or 'none yet'}.", "requests",
                             "We are actively working to add it."))
    parts.append('<section id="summary" class="plain"><h2>Summary</h2>'
                 '<p class="lead">We ran kohaku-cli wallet commands on a throwaway mainnet wallet through Kohaku\'s '
                 '<code>local-pir-rpc</code> proxy, which sends ETH balance lookups to the PIR server and every other '
                 'request to a public RPC. A patched proxy logged each request: which route served it, how long it '
                 'took, and how many bytes it moved. We then replayed the same traffic with and without PIR, and timed '
                 'each step of a PIR lookup to see where the time goes.</p>'
                 + ('<p class="note"><b>Testnet run (Sepolia), shadow mode.</b> Every balance and nonce request still '
                    'makes the full PIR lookup to the mainnet PIR server, timed and counted as below. The wallet gets '
                    'its answer from the Sepolia RPC, because the PIR server holds mainnet state only.</p>'
                    if meta.get("network") == "sepolia" else "")
                 + f'<div class="insights">{"".join(cards)}</div></section>')

    # --- request time per wallet action (side by side, not additive)
    groups, trows = [], []
    LOCAL_NOTES = {
        "fresh_addresses": "makes no RPC requests: deriving the next address is local",
        "create_wallet": "makes only no-private-input requests: a chain check and one block number, to record where "
                         "later stealth-address scans start",
    }
    for sname in order:
        es = by_s.get(sname, [])
        if not any(is_pir(e) for e in es):
            continue
        a = [e["total_ms"] for e in es if not is_pir(e) and sensitive(e)]
        g = [e["total_ms"] for e in es if not sensitive(e)]
        b = [e["total_ms"] for e in es if is_pir(e)]
        groups.append((sname, [pct(b, .5), pct(a, .5), pct(g, .5)],
                       [f"median of {len(b)} lookups", f"median of {len(a)} requests", f"median of {len(g)} requests"]))
        trows.append([sname, len(b), fmt_ms(pct(b, .5)) if b else "–", len(a), fmt_ms(pct(a, .5)) if a else "–",
                      len(g), fmt_ms(pct(g, .5)) if g else "–"])
    SER = ["PIR", "Public RPC, privacy-sensitive", "Public RPC, no private input"]
    COL = [C[0], C[1], "var(--insens)"]
    parts.append('<section id="requests"><h2>Time per request, by route</h2>'
                 '<p class="sub">Median time of one request in each wallet action. Bars are compared, not added.</p>'
                 '<ul class="keys"><li><b>Privacy-sensitive</b> requests carry your address, a specific note, or '
                 'your intent (which tokens or pairs you look up).</li>'
                 '<li><b>Gray</b> requests have no private input: chain id, block number, public protocol state. '
                 'Stealth-registry reads are gray too, because making them private needs OMR, not PIR.</li></ul>'
                 + legend(list(zip(SER, COL)))
                 + grouped_hbar(groups, SER, fmt_ms, colors=COL)
                 + "".join(f'<p class="sub">Not shown: {esc(n)} {esc(LOCAL_NOTES.get(n, "makes no PIR lookups"))} '
                           f'({len(by_s.get(n, []))} requests).</p>'
                           for n in order if not any(is_pir(e) for e in by_s.get(n, [])))
                 + table(["Wallet action", "PIR lookups", "Median", "Sensitive, public RPC", "Median",
                          "No private input", "Median"], trows)
                 + '<h3>What went to the public RPC</h3>'
                 '<p class="sub">Grouped by method and contract function. The last column names the dataset that would '
                 'let PIR serve each group. A gray event scan of a public contract still shows that you use the '
                 'protocol, but not who you are in it.</p>'
                 + legend([("Privacy-sensitive", C[1]), ("No private input", "var(--insens)")])
                 + stacked_hbar([(k, [v["sens"], v["n"] - v["sens"]], f"{fmt_ms(v['ms'])} total") for k, v in ranked[:14]],
                                ["privacy-sensitive", "no private input"], lambda x: f"{x:.0f}",
                                colors=[C[1], "var(--insens)"])
                 + table(["Request group", "Requests", "Share", "Privacy", "Top targets", "Private route"],
                         [[Raw(f'{esc(k)} <button type="button" class="info" aria-label="What is this" '
                               f'data-title="{esc(k)}" data-tip="{esc(describe(k))}">i</button>'),
                           v["n"], f"{v['n'] / len(fb):.0%}",
                           ("Sensitive: " if v["sens"] == v["n"] else f"{v['sens']} of {v['n']} sensitive: ")
                           + ", ".join(v["why"]) if v["sens"] else "No private input",
                           ", ".join(f"{t} ×{n}" for t, n in v["targets"].most_common(3)),
                           servability(k) if v["sens"] else (servability(k) if k.startswith("eth_call · stealth") else "not needed: no private input")]
                          for k, v in ranked], text_cols=(3, 4, 5))
                 + "</section>")

    inner = Counter()
    for e in fb:
        for ic in (e.get("call") or {}).get("inner", []):
            inner[f"{(ic.get('fn') or ic.get('selector', '?')).split('(')[0]} → {ic.get('to_label') or ic.get('to', '')[:10]}"] += 1
    if inner:
        parts.append('<section><h3>Calls inside Multicall batches</h3>'
                     + table(["Inner call → target", "Count"], inner.most_common(20)) + "</section>")

    # --- per-request latency
    src = (f"public RPC numbers are the identical requests sent direct in the baseline replay ({base_c} wallet)"
           if base else "no replay yet: the public RPC reference is all other requests in the drive")
    lat_groups = []
    for m in pir_methods:
        a = [e["total_ms"] for e in pir if e["method"] == m]
        b = [r["total_ms"] for r in base if r["method"] == m] or fb_ms
        lat_groups.append((m, [pct(a, .5), pct(b, .5)], [f"p95 {fmt_ms(pct(a, .95))}, n={len(a)}", f"p95 {fmt_ms(pct(b, .95))}, n={len(b)}"]))
    parts.append('<section id="latency"><h2>Per-request latency: PIR vs public RPC</h2>'
                 f'<p class="sub">Median time for the same request type over each route; {esc(src)}.</p>'
                 + legend([("PIR", C[0]), ("Public RPC", C[1])])
                 + grouped_hbar(lat_groups, ["PIR", "Public RPC"], fmt_ms)
                 + table(["Request", "PIR n", "PIR p50", "PIR p95", "Public n", "Public p50", "Public p95", "PIR / public"], lat_rows)
                 + '<h3>All requests</h3><p class="sub">Cumulative distribution, log scale: a point at (x, y) means '
                 'y of requests finished within x.</p>'
                 + legend([("PIR", C[0]), ("Public RPC", C[1])], "line")
                 + ecdf_chart([("PIR", pir_ms), ("Public RPC", fb_ms)])
                 + details_table(["Route", "n", "p50", "p90", "p95", "p99", "max"],
                                 [[n, len(xs), *(fmt_ms(pct(xs, q)) for q in (.5, .9, .95, .99, 1))]
                                  for n, xs in (("PIR", pir_ms), ("Public RPC", fb_ms))])
                 + "</section>")

    # --- wallet actions (replay)
    if sess:
        NAMES = {"pir": "With PIR", "baseline": "Without PIR"}
        labels = [l for l in ("pir", "baseline") if any(r["label"] == l for r in sess)]
        levels = sorted({r["concurrency"] for r in sess})
        with_pir = [x for x in order if any(is_pir(e) for e in by_s.get(x, [])) and any(r["session"] == x for r in sess)]
        wgroups, rows = [], []
        for sname in with_pir:
            v = [pct([r["wall_ms"] for r in sess if r["label"] == l and r["concurrency"] == c_lo and r["session"] == sname], .5) for l in labels]
            wgroups.append((sname, v, ["", ""]))
        for c in levels:
            for sname in with_pir:
                v = {l: pct([r["wall_ms"] for r in sess if r["label"] == l and r["concurrency"] == c and r["session"] == sname], .5) for l in labels}
                rows.append([sname, c, *(fmt_ms(v[l]) for l in labels),
                             f"{v['pir'] / v['baseline']:.1f}×" if v.get("pir") and v.get("baseline") else "–"])
        skipped = [x for x in order if x not in with_pir]
        parts.append('<section id="wallet"><h2>Wallet actions: with vs without PIR</h2>'
                     f'<p class="sub">The same recorded commands, replayed at {c_lo} wallet (median).</p>'
                     '<ul class="keys"><li><b>With PIR:</b> balance lookups over PIR, everything else over the public '
                     'RPC.</li><li><b>Without PIR:</b> everything over the public RPC.</li></ul>'
                     + (f'<p class="sub">Not shown: {esc(", ".join(skipped))} (no PIR lookups).</p>' if skipped else "")
                     + legend([(NAMES[l], C[i]) for i, l in enumerate(labels)])
                     + grouped_hbar(wgroups, [NAMES[l] for l in labels], fmt_ms)
                     + details_table(["Wallet action", "Parallel wallets", *(NAMES[l] for l in labels), "Ratio"], rows,
                                     summary="All load levels")
                     + "</section>")

    # --- inside a lookup: where to optimize
    have = [e["pir"] for e in pir if "pir" in e]
    if have:
        side = [(e["ts"], e["pir"]) for e in pir if "pir" in e]
        t0 = side[0][0]
        body = '<section id="lookup"><h2>Inside a PIR lookup: where the time goes</h2>'
        if steps:
            d = dict((n, v) for n, v, _ in steps)
            levers = {
                "Upload": f"{fmt_bytes(pct(wire_up, .5))} per lookup, effective ~{up_bps * 8 / 1e6:.1f} Mbit/s on a link "
                          "that does ~85 Mbit/s. The gap is TCP slow start: a new connection per lookup on a 150 ms path "
                          "needs about 6 round trips to send 760 KB. Levers: reuse the connection and turn off TCP's "
                          "idle restart (measured below), then smaller queries (research).",
                "Download": f"{fmt_bytes(pct(wire_dn, .5))}, effective ~{dn_bps * 8 / 1e6:.1f} Mbit/s, also slow start; "
                            f"{side_share:.0%} of it is the sidecar, JSON with hex fields (twice the raw bytes), resent on "
                            "every lookup. Levers: the same TCP settings on the server; a binary sidecar sent once per "
                            "block; more frequent database rebuilds.",
                "Network round trip": "Distance to the server (Netherlands, ~150 ms). Paid on every lookup, and lookups "
                                      "run one after another. Levers: batch several addresses per request, parallel "
                                      "lookups, a server closer to users.",
                "Server compute": "The GPU answer. Small at this load.",
                "Build query (client)": "Client CPU. Negligible.",
                "Decode (client)": "Client CPU. Negligible.",
            }
            body += ('<p class="sub">One lookup, step by step (median). Upload and round trip come from a probe the server '
                     'rejects without GPU work; server compute is the rest of the wait for the first byte.</p>'
                     + legend([(n, C[i]) for i, (n, _, _) in enumerate(steps)])
                     + stacked_hbar([("one lookup", [v for _, v, _ in steps], "")], [n for n, _, _ in steps], fmt_ms)
                     + table(["Step", "Median", "Share", "What drives it and what to optimize"],
                             [[n, fmt_ms(v), f"{v / steps_total:.0%}", levers[n]]
                              for n, v, _ in sorted(steps, key=lambda x: -x[1])], text_cols=(3,))
                     + (('<h3>Experiment: the same lookup with better TCP handling</h3>'
                         f'<p class="sub">This machine does ~{tcp["bandwidth"]["cloudflare_down_mbit"]} Mbit/s each way; the PIR '
                         f'server is {tcp["bandwidth"]["pir_rtt_ms"]} ms away. Same server and scheme, '
                         f'{tcp["rows"][0]["n"]} lookups per row. Only the client side was changed.</p>'
                         + table(["Setup", "What changed", "Lookup", "Send + server + first byte", "Download"],
                                 [[r["label"], r["detail"], fmt_ms(r["lookup_ms"]), fmt_ms(r["http_ms"]),
                                   fmt_ms(r["body_ms"])] for r in tcp["rows"]], num_from=2)) if tcp else ""))
        body += ('<h3>Bytes per lookup over time</h3>'
                 '<p class="sub">Each response carries the sidecar: all account changes since the server last rebuilt '
                 'its database. It grows every block and resets at each rebuild, hence the sawtooth. Each dot is one '
                 'lookup; the gap is the Railgun scenario downloading its own data.</p>'
                 + legend([("Response bytes", C[0]), ("of which sidecar", C[1])], "line")
                 + line_chart([("Response", [((t - t0) / 1e3, p_["resp_bytes"]) for t, p_ in side]),
                               ("Sidecar", [((t - t0) / 1e3, p_["sidecar_bytes"]) for t, p_ in side])],
                              "seconds into run", fmt_bytes, x_fmt=lambda x: f"{x:.0f}")
                 + table(["", "PIR (p50)", "PIR (max)", "Same request over public RPC (p50)", "Ratio"],
                         [["Upload", fmt_bytes(pct(wire_up, .5)), fmt_bytes(max(wire_up)), fmt_bytes(pct(rpc_up, .5)),
                           f"{pct(wire_up, .5) / pct(rpc_up, .5):,.0f}×"],
                          ["Download", fmt_bytes(pct(wire_dn, .5)), fmt_bytes(max(wire_dn)), fmt_bytes(pct(rpc_dn, .5)),
                           f"{pct(wire_dn, .5) / pct(rpc_dn, .5):,.0f}×"],
                          ["Sidecar entries", f"{pct([p_['sidecar_entries'] for _, p_ in side], .5):.0f}",
                           f"{max(p_['sidecar_entries'] for _, p_ in side)}", "", ""],
                          ["PIR data behind chain head", f"{pct(lags, .5):.0f} blocks" if lags else "–",
                           f"{max(lags)} blocks" if lags else "–", "", ""]])
                 + '<h3>Phases as seen by the proxy</h3>'
                 + details_table(["Phase", "p50", "p95", "max"],
                                 [[n, *(fmt_ms(pct([p_[k] for p_ in have], q)) for q in (.5, .95, 1))]
                                  for k, n in [("lock_wait_ms", "Lock wait"), ("query_build_ms", "Build query"),
                                               ("http_ms", "Send + server + first byte"), ("body_ms", "Download"),
                                               ("decode_ms", "Decode")]], summary="Table view")
                 + "</section>")
        parts.append(body)

    # --- load
    load_html = ""
    if load_levels:
        errs = Counter(r["concurrency"] for r in pbench if r.get("kind") == "lookup" and not r.get("ok"))
        load_html += ('<p class="sub">Several simulated wallets at once: does the server keep up, and does the proxy?</p>'
                      '<h3>PIR server, direct</h3><p class="sub">Each wallet has its own PIR client. Lookups stay '
                      'around 1.5–2.5 s from 1 to 16 wallets, so the server is not the limit.</p>'
                      + legend([("p50", C[0]), ("p95", C[1])], "line")
                      + line_chart([("p50", [(c, pct(by_c[c], .5)) for c in load_levels]),
                                    ("p95", [(c, pct(by_c[c], .95)) for c in load_levels])],
                                   "parallel wallets", fmt_ms)
                      + details_table(["Parallel wallets", "Lookups", "Errors", "p50", "p95", "max", "Lookups / s"],
                              [[c, len(by_c[c]), errs.get(c, 0), fmt_ms(pct(by_c[c], .5)), fmt_ms(pct(by_c[c], .95)),
                                fmt_ms(max(by_c[c])), f"{thr[c]['throughput_per_s']:.2f}" if c in thr else "–"]
                               for c in load_levels]))
    if sess:
        err_ref, level_rows, lg = {}, [], {}
        for c in sorted({r["concurrency"] for r in sess}):
            lock = [e["pir"]["lock_wait_ms"] for e in replay_ev
                    if "pir" in e and e.get("session", "").startswith(f"replay:pir:c{c}:")]
            for l in labels:
                rs_ = [r for r in rrpc if r["label"] == l and r["concurrency"] == c]
                er = sum(not r["ok"] for r in rs_) / len(rs_) if rs_ else 0
                if l not in err_ref:
                    err_ref[l] = er
                valid = "yes" if er <= err_ref[l] + 0.05 else "no: errors above the 1-wallet level"
                ws = [r["wall_ms"] for r in sess if r["label"] == l and r["concurrency"] == c]
                lg.setdefault(c, {})[l] = (pct(ws, .5), valid == "yes")
                level_rows.append([c, NAMES[l], len(rs_), f"{er:.0%}", fmt_ms(pct(ws, .5)),
                                   fmt_ms(pct(lock, .5)) if l == "pir" and lock else "–",
                                   fmt_ms(max(lock)) if l == "pir" and lock else "–", valid])
        load_groups = [(f"{c} wallet" + ("s" if c > 1 else ""),
                        [lg[c].get("pir", (None,))[0], lg[c].get("baseline", (None,))[0]],
                        ["", "" if lg[c].get("baseline", (0, True))[1] else "rate-limited, not comparable"],
                        [None, None if lg[c].get("baseline", (0, True))[1] else "var(--insens)"])
                       for c in sorted(lg)]
        load_html += ('<h3>Through the proxy</h3><p class="sub">All wallets share the proxy\'s single PIR client, so '
                      'lookups wait in line and sessions get longer with each wallet. Gray bars are rate-limited by '
                      'the public RPC and not a fair comparison.</p>'
                      + legend([("With PIR", C[0]), ("Without PIR", C[1]), ("Without PIR, rate-limited", "var(--insens)")])
                      + grouped_hbar(load_groups, ["With PIR", "Without PIR"], fmt_ms, colors=[C[0], C[1]])
                      + details_table(["Parallel wallets", "Replay", "Requests", "Error rate", "Median session",
                                       "Proxy lock wait p50", "Lock wait max", "Valid"], level_rows, text_cols=(1, 7)))
    if load_html:
        parts.append(f'<section id="load"><h2>Under load</h2>{load_html}</section>')

    # --- method & run
    REPOS = {"kohaku-cli": "kassandraoftroy/kohaku-cli", "local-pir-rpc": "kassandraoftroy/local-pir-rpc",
             "kohaku-rs": "ethereum/kohaku-rs", "inspire-gpu-serving": "keewoolee/inspire-gpu-serving"}
    ver_rows = "".join(
        f'<tr><td>{esc(k)}</td><td><a href="https://github.com/{REPOS[k]}/commit/{esc(v)}">{esc(v)}</a></td></tr>'
        for k, v in meta.items() if k in REPOS)
    run_rows = [["Started (UTC)", meta.get("started_utc", "–")], ["Machine", meta.get("host", "–")],
                ["PIR server", meta.get("pir_url", "–")], ["Public RPC", meta.get("fallback_rpc", "–")],
                ["Requests logged", len(events)], ["Chain head (last seen)", heads[-1] if heads else "–"],
                ["PIR snapshot blocks seen", f"{min(snaps)}–{max(snaps)}" if snaps else "–"]]
    parts.append('<section id="run"><h2>Method &amp; run</h2>'
                 + '<details class="method"><summary>How we measured</summary>' + METHOD + '</details>'
                 + '<p class="sub">To reproduce this run, see <a href="https://github.com/andyguzmaneth/pir-wallet-bench">'
                   'github.com/andyguzmaneth/pir-wallet-bench</a>.</p>'
                 + '<h3>Run</h3>' + table(["Field", "Value"], run_rows, num_from=9)
                 + '<h3>Software versions</h3><p class="sub">The git commit of each codebase this run used, linked to '
                   'GitHub, so the run can be reproduced exactly.</p>'
                 + f'<table><thead><tr><th>Codebase</th><th>Commit</th></tr></thead><tbody>{ver_rows}</tbody></table>'
                 + "</section>")

    summary.update({
        "pir_balance_p50_ms": bal_pir, "public_balance_p50_ms": bal_pub,
        "network": meta.get("network", "mainnet"), "label": meta.get("label", ""), "note": meta.get("note", ""),
        "started_utc": meta.get("started_utc", ""),
        "sensitive_requests": len(sens), "sensitive_share_pir": len(pir) / len(sens) if sens else None,
        "insensitive_requests": len(insens),
        "lookup_steps_ms": {n: v for n, v, _ in steps} if steps else None,
        "wallet_action_ratio_min": min((r[1] / r[2] for r in ratios), default=None),
        "wallet_action_ratio_max": max((r[1] / r[2] for r in ratios), default=None),
        "direct_load_p50_ms": {str(c): pct(by_c[c], .5) for c in load_levels},
        "proxy_lock_wait_p50_ms_at_max": proxy_hi[3] if proxy_hi else None,
        "pir_methods": dict(Counter(e["method"] for e in pir)),
    })
    nav_html = "".join(f'<a href="#{i}">{esc(t)}</a>' for i, t in nav if f'id="{i}"' in "".join(parts))
    parts.insert(0, f'<nav id="toc">{nav_html}</nav>')
    (run / "summary.json").write_text(json.dumps(summary, indent=1))
    title = f"PIR wallet bench · {run.name}" + (f" · {meta['label']}" if meta.get("label") else "")
    (run / "report.html").write_text(PAGE.replace("{{TITLE}}", esc(title)).replace("{{BODY}}", "\n".join(parts)))
    return run / "report.html"


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{TITLE}}</title>
<style>
:root{color-scheme:light;--bg:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;--s6:#008300;--insens:#b5b3ab}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--insens:#5f5e5a}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--insens:#5f5e5a}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:980px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 6px}h2{font-size:17px;margin:0 0 10px}h3{font-size:14px;margin:32px 0 8px}
html{scroll-behavior:smooth}section{scroll-margin-top:64px}
#toc{position:sticky;top:0;z-index:5;display:flex;gap:4px;overflow-x:auto;padding:10px 0;margin:8px 0 0;background:var(--bg);border-bottom:1px solid var(--border);scrollbar-width:none}
#toc a{flex:none;color:var(--ink2);text-decoration:none;font-size:13px;padding:5px 10px;border-radius:999px}
#toc a:hover{background:var(--grid)}#toc a.on{background:var(--ink);color:var(--bg)}
section.plain{background:none;border:0;padding:0}
.insights{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px;margin-top:12px}
.card{display:flex;flex-direction:column;text-decoration:none;color:inherit;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px 18px}
.card:hover{border-color:var(--axis)}.card{position:relative}
.todo{display:flex;align-items:center;gap:6px;margin-top:12px;padding-top:10px;border-top:1px solid var(--grid);font-size:13px}
.todo b{font-weight:600;color:var(--ink)}
td .info{display:inline-block;vertical-align:-4px;margin-left:4px;width:18px;height:18px;font-size:11px;line-height:16px}
.info{width:20px;height:20px;border-radius:50%;border:1px solid var(--axis);background:var(--surface);color:var(--ink2);font:italic 600 12px/18px Georgia,serif;cursor:help;padding:0}
.info:hover,.info:focus-visible,.info.open{background:var(--ink);color:var(--bg);border-color:var(--ink);outline:none}
.ct{font-weight:600;font-size:14px}.cv{font-size:22px;font-weight:600;margin:6px 0 4px}.cd{color:var(--ink2);font-size:13px;line-height:1.55;flex:1}
.todo{margin-top:10px;padding-top:8px;border-top:1px solid var(--grid);font-size:13px;color:var(--ink)}.todo span{display:block;font-size:11px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);margin-bottom:2px}
details.method{margin:6px 0 4px}details.method summary{font-size:14px;color:var(--ink)}details.method p{color:var(--ink2);line-height:1.6}
.note{max-width:80ch;margin:8px 0 12px;padding:8px 12px;border-left:3px solid var(--axis);background:var(--surface);color:var(--ink2);font-size:13px}
.lead{margin:4px 0 14px;color:var(--ink2);line-height:1.6}a{color:var(--s1)}
code{font-size:12.5px;background:var(--grid);padding:1px 4px;border-radius:4px}
.sub,.muted{color:var(--ink2);margin:0 0 14px;line-height:1.6}
.keys{color:var(--ink2);margin:-6px 0 14px;padding-left:18px;line-height:1.6}.keys li{margin:2px 0}.keys b{color:var(--ink);font-weight:600}
section{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:24px 24px 18px;margin:20px 0;overflow-x:auto}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;background:none;border:0;padding:0}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px}
.tl{color:var(--ink2);font-size:13px}.tv{font-size:24px;font-weight:600;margin:4px 0}.ts{color:var(--muted);font-size:12px}
.chart{width:100%;height:auto;display:block;margin:4px 0 8px}
.grid{stroke:var(--grid);stroke-width:1}.axis{stroke:var(--axis);stroke-width:1}
.tick{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}.lab{fill:var(--ink2);font-size:12px}.val{fill:var(--ink);font-size:12px}
.ring{stroke:var(--surface);stroke-width:2}.mark{cursor:default}.mark:hover,.mark:focus{opacity:.8;outline:none}
.xhair{stroke:var(--muted);stroke-width:1}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin:4px 0;color:var(--ink2);font-size:12px}
.lg{display:inline-flex;align-items:center;gap:6px}.key.rect{width:10px;height:10px;border-radius:2px}.key.line{width:14px;height:2px}
table{border-collapse:collapse;width:100%;margin:10px 0 16px;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--grid);vertical-align:top}
th{color:var(--ink2);font-weight:500}.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
details{margin:8px 0 4px}details summary{cursor:pointer;color:var(--ink2);font-size:13px;margin:6px 0}
#tip{position:fixed;pointer-events:none;background:var(--surface);color:var(--ink);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,.12);display:none;max-width:320px;z-index:10}
#tip b{font-weight:600}
</style></head><body><main>
<h1>{{TITLE}}</h1>
<p class="sub">kohaku-cli sessions through local-pir-rpc. PIR serves latest-block ETH balance and nonce for EOAs; everything else goes to a public RPC.</p>
{{BODY}}
</main><div id="tip"></div>
<script>
(function(){
  var links={};document.querySelectorAll('#toc a').forEach(function(a){links[a.getAttribute('href').slice(1)]=a;});
  if('IntersectionObserver' in window){var io=new IntersectionObserver(function(es){es.forEach(function(e){
    if(e.isIntersecting){Object.values(links).forEach(function(a){a.classList.remove('on');});var a=links[e.target.id];
    if(a){a.classList.add('on');a.scrollIntoView({block:'nearest',inline:'nearest'});}}});},{rootMargin:'-70px 0px -70% 0px'});
    Object.keys(links).forEach(function(id){var el=document.getElementById(id);if(el)io.observe(el);});}
  var tip=document.getElementById('tip');
  document.querySelectorAll('.info[data-tip]').forEach(function(b){
    function at(){var r=b.getBoundingClientRect();tip.textContent='';var h=document.createElement('b');h.textContent=b.getAttribute('data-title')||'';
      h.style.display='block';h.style.marginBottom='4px';tip.appendChild(h);tip.appendChild(document.createTextNode(b.getAttribute('data-tip')));
      tip.style.whiteSpace='normal';tip.style.lineHeight='1.5';tip.style.padding='10px 12px';tip.style.display='block';
      tip.style.maxWidth='300px';var w=tip.offsetWidth,h2=tip.offsetHeight;tip.style.left=Math.max(8,Math.min(r.left-12,innerWidth-w-8))+'px';
      tip.style.top=((r.bottom+8+h2>innerHeight)?(r.top-h2-8):(r.bottom+8))+'px';}
    function off(){if(!b.classList.contains('open'))tip.style.display='none';}
    b.addEventListener('pointerenter',at);b.addEventListener('pointerleave',off);
    b.addEventListener('focus',at);b.addEventListener('blur',function(){b.classList.remove('open');tip.style.display='none';});
    b.addEventListener('click',function(e){e.preventDefault();e.stopPropagation();
      document.querySelectorAll('.info.open').forEach(function(o){if(o!==b)o.classList.remove('open');});
      b.classList.toggle('open');if(b.classList.contains('open'))at();else tip.style.display='none';});
  });
  document.addEventListener('scroll',function(){document.querySelectorAll('.info.open').forEach(function(o){o.classList.remove('open');});tip.style.display='none';},{passive:true});
  function show(t,x,y){tip.textContent=t;tip.style.padding='';tip.style.lineHeight='';tip.style.display='block';var w=tip.offsetWidth;tip.style.left=Math.min(x+12,innerWidth-w-8)+'px';tip.style.top=(y+12)+'px';}
  function hide(){tip.style.display='none';}
  document.querySelectorAll('.mark[data-tip]').forEach(function(m){
    m.addEventListener('pointermove',function(e){show(m.getAttribute('data-tip'),e.clientX,e.clientY);});
    m.addEventListener('pointerleave',hide);
    m.addEventListener('focus',function(){var r=m.getBoundingClientRect();show(m.getAttribute('data-tip'),r.right,r.top);});
    m.addEventListener('blur',hide);
  });
  document.querySelectorAll('svg.ecdf').forEach(function(svg){
    var hit=svg.querySelector('.hit'),xh=svg.querySelector('.xhair');
    var series=[].map.call(svg.querySelectorAll('.ecdf-data'),function(g){return {label:g.getAttribute('data-label'),pts:JSON.parse(g.getAttribute('data-pts'))};});
    function fmt(v){return v>=1000?(v/1000).toFixed(2)+' s':Math.round(v)+' ms';}
    hit.addEventListener('pointermove',function(e){
      var p=svg.createSVGPoint();p.x=e.clientX;p.y=e.clientY;var q=p.matrixTransform(svg.getScreenCTM().inverse());
      xh.setAttribute('x1',q.x);xh.setAttribute('x2',q.x);xh.setAttribute('visibility','visible');
      var lines=series.map(function(s){var n=0,v=null;s.pts.forEach(function(pt){if(pt[0]<=q.x){n++;v=pt[1];}});
        return (Math.round(100*n/s.pts.length))+'% of '+s.label+' within '+(v===null?'–':fmt(v));});
      show(lines.join('\\n'),e.clientX,e.clientY);tip.style.whiteSpace='pre';
    });
    hit.addEventListener('pointerleave',function(){xh.setAttribute('visibility','hidden');hide();tip.style.whiteSpace='';});
  });
})();
</script></body></html>
"""

if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: report.py <run-dir>")
    print(build(Path(sys.argv[1])))

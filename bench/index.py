#!/usr/bin/env python3
"""Build the run index: every run with its label and key numbers, plus a
side-by-side comparison of any two runs (chosen in the page).

usage: index.py <site-dir>   reads runs/*/summary.json and meta.json, writes <site-dir>/index.html
"""

import html
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"

# (key, label, unit, better) — better: "lower", "higher" or "" (informational)
METRICS = [
    ("sensitive_share_pir", "Privacy-sensitive requests served by PIR", "pct", "higher"),
    ("pir_balance_p50_ms", "PIR lookup, median", "ms", "lower"),
    ("public_balance_p50_ms", "Same request over the public RPC, median", "ms", "lower"),
    ("pir_p95_ms", "PIR lookup, p95", "ms", "lower"),
    ("step:Upload", "Lookup step: upload", "ms", "lower"),
    ("step:Download", "Lookup step: download", "ms", "lower"),
    ("step:Server compute", "Lookup step: server compute", "ms", "lower"),
    ("step:Network round trip", "Lookup step: round trip", "ms", "lower"),
    ("pir_req_bytes_p50", "Upload per lookup", "bytes", "lower"),
    ("pir_resp_bytes_p50", "Download per lookup", "bytes", "lower"),
    ("wallet_action_ratio_min", "Wallet action with vs without PIR, best", "x", "lower"),
    ("wallet_action_ratio_max", "Wallet action with vs without PIR, worst", "x", "lower"),
    ("proxy_lock_wait_p50_ms_at_max", "Proxy queue wait at highest load, median", "ms", "lower"),
    ("load:16", "Direct PIR lookup at 16 wallets, median", "ms", "lower"),
    ("token_pir_requests", "Token balances served by PIR", "n", "higher"),
    ("pir_cache_hits", "PIR answers from the proxy cache", "n", ""),
    ("requests", "Requests logged", "n", ""),
    ("pir_requests", "PIR lookups", "n", ""),
    ("sensitive_requests", "Privacy-sensitive requests", "n", ""),
    ("snapshot_lag_blocks_p50", "PIR data behind chain head", "blocks", "lower"),
]


def value(s, key):
    if key.startswith("step:"):
        return (s.get("lookup_steps_ms") or {}).get(key[5:])
    if key.startswith("load:"):
        return (s.get("direct_load_p50_ms") or {}).get(key[5:])
    return s.get(key)


def main():
    site = Path(sys.argv[1])
    runs = []
    for d in sorted(RUNS.iterdir()):
        if d.name.startswith("_") or not (d / "summary.json").exists():
            continue
        s = json.loads((d / "summary.json").read_text())
        meta = json.loads((d / "meta.json").read_text()) if (d / "meta.json").exists() else {}
        runs.append({
            "id": d.name, "started": meta.get("started_utc", s.get("started_utc", "")),
            "network": meta.get("network", "mainnet"), "label": meta.get("label", ""), "note": meta.get("note", ""),
            "proxy": meta.get("proxy_under_test", "bench-instrumented local-pir-rpc"),
            "metrics": {k: value(s, k) for k, *_ in METRICS},
        })
    runs.sort(key=lambda r: r["started"], reverse=True)
    data = json.dumps({"runs": runs, "metrics": METRICS}).replace("</", "<\\/")
    (site / "index.html").write_text(PAGE.replace("{{DATA}}", data))
    print(site / "index.html")


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>PIR wallet bench runs</title>
<style>
:root{color-scheme:light;--bg:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--grid:#e1e0d9;--border:rgba(11,11,11,.10);--good:#006300;--bad:#b32c2c;--link:#2a78d6}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--border:rgba(255,255,255,.10);--good:#0ca30c;--bad:#e66767;--link:#86b6ef}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--border:rgba(255,255,255,.10);--good:#0ca30c;--bad:#e66767;--link:#86b6ef}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1000px;margin:0 auto;padding:28px 16px 64px}h1{font-size:22px;margin:0 0 6px}h2{font-size:17px;margin:0 0 10px}
.sub{color:var(--ink2);margin:0 0 14px}a{color:var(--link)}
section{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:22px 24px 16px;margin:20px 0;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px;margin:6px 0}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--grid);vertical-align:top}
th{color:var(--ink2);font-weight:500}.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.pick{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin:0 0 12px}select{font:inherit;padding:5px 8px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--ink);max-width:100%}
.good{color:var(--good)}.bad{color:var(--bad)}.tag{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
</style></head><body><main>
<h1>PIR wallet bench runs</h1>
<p class="sub">Each run drives kohaku-cli wallet commands through the PIR proxy. Pick two runs to compare them.</p>
<section><h2>Compare two runs</h2>
<div class="pick"><label>Before <select id="a"></select></label><label>After <select id="b"></select></label></div>
<table id="cmp"><thead><tr><th>Metric</th><th class="num">Before</th><th class="num">After</th><th class="num">Change</th></tr></thead><tbody></tbody></table>
<p class="sub" id="cmpnote"></p></section>
<section><h2>All runs</h2><table id="runs"><thead><tr><th>Run</th><th>Network</th><th>Proxy</th><th>Label</th><th class="num">PIR lookup</th><th class="num">Sensitive served by PIR</th><th>Report</th></tr></thead><tbody></tbody></table></section>
</main>
<script id="data" type="application/json">{{DATA}}</script>
<script>
(function(){
  var D=JSON.parse(document.getElementById('data').textContent), R=D.runs, M=D.metrics;
  function fmt(v,u){ if(v===null||v===undefined) return '–';
    if(u==='ms') return v>=1000?(v/1000).toFixed(2)+' s':Math.round(v)+' ms';
    if(u==='bytes') return v>=1e6?(v/1e6).toFixed(1)+' MB':v>=1e3?(v/1e3).toFixed(1)+' KB':Math.round(v)+' B';
    if(u==='pct') return Math.round(v*100)+'%'; if(u==='x') return v.toFixed(1)+'×';
    return String(Math.round(v*10)/10); }
  function td(t,cls){var e=document.createElement('td');e.textContent=t;if(cls)e.className=cls;return e;}
  function name(r){return r.id+(r.label?' · '+r.label:'');}
  var a=document.getElementById('a'), b=document.getElementById('b');
  R.forEach(function(r,i){[a,b].forEach(function(s){var o=document.createElement('option');o.value=i;o.textContent=name(r);s.appendChild(o);});});
  var base=R.findIndex(function(r){return /baseline/i.test(r.label);});
  a.value=base>=0?base:Math.min(1,R.length-1); b.value=0;
  function draw(){
    var A=R[a.value], B=R[b.value], tb=document.querySelector('#cmp tbody'); tb.textContent='';
    M.forEach(function(m){ var k=m[0],lab=m[1],u=m[2],better=m[3], va=A.metrics[k], vb=B.metrics[k];
      if((va===null||va===undefined)&&(vb===null||vb===undefined)) return;
      var tr=document.createElement('tr'); tr.appendChild(td(lab)); tr.appendChild(td(fmt(va,u),'num')); tr.appendChild(td(fmt(vb,u),'num'));
      var ch='–',cls='num';
      if(typeof va==='number'&&typeof vb==='number'&&va!==0){ var d=(vb-va)/Math.abs(va); ch=(d>0?'+':'')+Math.round(d*100)+'%';
        if(better&&Math.abs(d)>=0.05){ var good=(better==='lower')?d<0:d>0; ch+=good?' better':' worse'; cls+=good?' good':' bad'; } }
      tr.appendChild(td(ch,cls)); tb.appendChild(tr); });
    var n=document.getElementById('cmpnote'); n.textContent='';
    [A,B].forEach(function(r,i){ if(r.note){ var p=document.createElement('div'); p.textContent=(i?'After':'Before')+' ('+r.id+'): '+r.note; n.appendChild(p);} });
    if(A.network!==B.network){ var w=document.createElement('div'); w.textContent='Different networks: '+A.network+' vs '+B.network+'. PIR numbers compare directly; public-RPC numbers do not.'; n.appendChild(w); }
  }
  a.onchange=b.onchange=draw; draw();
  var rb=document.querySelector('#runs tbody');
  R.forEach(function(r){ var tr=document.createElement('tr'); tr.appendChild(td(r.id)); tr.appendChild(td(r.network)); tr.appendChild(td(r.proxy||'–'));
    var l=td(r.label||'–'); if(r.note){var s=document.createElement('div');s.className='tag';s.textContent=r.note;l.appendChild(s);} tr.appendChild(l);
    tr.appendChild(td(fmt(r.metrics.pir_balance_p50_ms,'ms'),'num')); tr.appendChild(td(fmt(r.metrics.sensitive_share_pir,'pct'),'num'));
    var c=document.createElement('td'), x=document.createElement('a'); x.href=r.id+'.html'; x.textContent='open'; c.appendChild(x); tr.appendChild(c); rb.appendChild(tr); });
})();
</script></body></html>
"""

if __name__ == "__main__":
    main()

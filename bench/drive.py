#!/usr/bin/env python3
"""Drive kohaku-cli through wallet scenarios against the bench proxy.

Each scenario tags the proxy session first (POST /_bench/session), so every
RPC event in events.jsonl carries the scenario that caused it. The wallet is a
throwaway: its data dir and password live inside the run directory.

Output: <run>/drive.jsonl (one line per scenario) and <run>/drive/<name>.log.
"""

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WALLET = "bench"

# name -> list of kohaku argv tails. {common} expands to the shared flags.
SCENARIOS = {
    "create_wallet": [["create-wallet", "{wallet}", "--non-interactive", "{pw}", "--rpc-url", "{rpc}", "{dd}"]],
    "fresh_addresses": [["next-fresh-address", "--wallet", "{wallet}", "--non-interactive", "{pw}", "{dd}"]],
    "balances_public": [["balances", "{common}", "--skip-stealth-scan"]],
    "balances_stealth": [["balances", "{common}"]],
    "balances_tornado": [["balances", "{common}", "--skip-stealth-scan", "--include", "tornado"]],
    "balances_privacy_pools": [["balances", "{common}", "--skip-stealth-scan", "--include", "privacy-pools"]],
    "balances_railgun": [["balances", "{common}", "--skip-stealth-scan", "--include", "railgun"]],
    "balances_all_warm": [["balances", "{common}", "--include", "tornado,privacy-pools,railgun"]],
    "see_stealth_meta_address": [["see-stealth-meta-address", "--wallet", "{wallet}", "--non-interactive", "{pw}", "{dd}"]],
    # Send flows (testnet wallet with funds). Without --broadcast, kohaku-cli
    # still reads balance, nonce, the 7702 delegation (eth_getCode) and gas.
    "send_simulate": [["transfer", "{common}", "--from", "0", "--to", "{to}", "--amount-formatted", "0.0001"]],
    "send_broadcast": [["transfer", "{common}", "--from", "0", "--to", "{to}", "--amount-formatted", "0.0001",
                        "--broadcast"]],
}
SCENARIOS["balances_after_send"] = SCENARIOS["balances_public"]
SEPOLIA_ORDER = ["fresh_addresses", "balances_public", "send_simulate", "send_broadcast", "balances_after_send"]
DEFAULT_ORDER = [
    "create_wallet",
    "fresh_addresses",
    "balances_public",
    "balances_stealth",
    "balances_tornado",
    "balances_privacy_pools",
    "balances_railgun",
    "balances_all_warm",
]


def set_session(proxy: str, name: str) -> None:
    req = urllib.request.Request(
        proxy.rstrip("/") + "/_bench/session",
        data=json.dumps({"session": name}).encode(),
        headers={"content-type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=10).read()


def expand(tail, ctx):
    out = []
    for a in tail:
        if a == "{common}":
            out += ["--wallet", WALLET, "--non-interactive", *ctx["pw"], "--rpc-url", ctx["rpc"], *ctx["dd"]]
        elif a in ("{pw}", "{dd}"):
            out += ctx[a[1:-1]]
        elif a == "{rpc}":
            out.append(ctx["rpc"])
        elif a == "{wallet}":
            out.append(WALLET)
        elif a == "{to}":
            out.append(ctx["to"])
        else:
            out.append(a)
    return out


def main() -> int:
    global WALLET
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True, type=Path)
    ap.add_argument("--proxy", default="http://127.0.0.1:18545")
    ap.add_argument("--scenarios", default=",".join(DEFAULT_ORDER),
                    help=f"comma list; known: {','.join(SCENARIOS)}")
    ap.add_argument("--fresh", type=int, default=5, help="next-fresh-address repetitions")
    ap.add_argument("--timeout", type=int, default=1800, help="seconds per command")
    ap.add_argument("--node", default=str(ROOT / ".tools/node/bin/node"))
    ap.add_argument("--kohaku", default=str(ROOT / "vendor/kohaku-cli/bin/kohaku.mjs"))
    ap.add_argument("--tor", action="store_true", help="keep Tor for non-RPC HTTP (slower; RPC is clearnet either way)")
    ap.add_argument("--network", choices=["mainnet", "sepolia"], default="mainnet")
    ap.add_argument("--wallet-dir", type=Path,
                    help="reuse a persistent wallet (data/ + password inside), e.g. a funded testnet wallet")
    ap.add_argument("--wallet-name", default=WALLET)
    args = ap.parse_args()

    run = args.run_dir
    (run / "drive").mkdir(parents=True, exist_ok=True)
    WALLET = args.wallet_name
    if args.wallet_dir:
        data_dir, pw_file = args.wallet_dir / "data", args.wallet_dir / "password"
    else:
        data_dir, pw_file = run / "kohaku-data", run / "wallet-password"
        if not pw_file.exists():
            pw_file.write_text(secrets.token_hex(16))
            pw_file.chmod(0o600)
    ctx = {"pw": ["--password-file", str(pw_file)], "dd": ["--dataDir", str(data_dir)], "rpc": args.proxy}

    env = dict(os.environ)
    env["PATH"] = str(Path(args.node).parent) + os.pathsep + env.get("PATH", "")
    env["RPC_URL"] = args.proxy
    env["KOHAKU_NO_WORKER_PROGRESS"] = "1"
    if not args.tor:
        env["KOHAKU_WITHOUT_TOR"] = "1"

    if args.network == "sepolia" and args.scenarios == ",".join(DEFAULT_ORDER):
        args.scenarios = ",".join(SEPOLIA_ORDER)
    names = [s for s in args.scenarios.split(",") if s]
    if any(n.startswith("send_") for n in names):
        # Recipient: the wallet's own next address, so test funds stay in the wallet.
        peek = subprocess.run([args.node, args.kohaku, "next-fresh-address", "--peek", "--wallet", WALLET,
                               "--non-interactive", *ctx["pw"], *ctx["dd"]],
                              capture_output=True, text=True, env=env, timeout=120)
        import re
        m = re.findall(r"0x[0-9a-fA-F]{40}", peek.stdout)
        if not m:
            print(f"could not derive a recipient address: {peek.stdout[-300:]} {peek.stderr[-300:]}", file=sys.stderr)
            return 2
        ctx["to"] = m[-1]
    unknown = [s for s in names if s not in SCENARIOS]
    if unknown:
        print(f"unknown scenarios: {unknown}", file=sys.stderr)
        return 2
    if "create_wallet" not in names and not (data_dir.exists() and any(data_dir.iterdir())):
        names.insert(0, "create_wallet")

    failures = 0
    with open(run / "drive.jsonl", "a") as out:
        for name in names:
            steps = SCENARIOS[name]
            if name == "fresh_addresses":
                steps = steps * args.fresh
            set_session(args.proxy, name)
            t0 = time.time()
            codes = []
            log_path = run / "drive" / f"{name}.log"
            with open(log_path, "w") as log:
                for tail in steps:
                    argv = [args.node, args.kohaku, *expand(tail, ctx)]
                    log.write(f"$ kohaku {' '.join(argv[2:])}\n")
                    log.flush()
                    try:
                        p = subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT, env=env,
                                           timeout=args.timeout, stdin=subprocess.DEVNULL)
                        codes.append(p.returncode)
                    except subprocess.TimeoutExpired:
                        log.write(f"\n[bench] timeout after {args.timeout}s\n")
                        codes.append("timeout")
            wall = time.time() - t0
            ok = all(c == 0 for c in codes)
            failures += not ok
            rec = {"ts": t0 * 1e3, "scenario": name, "wall_s": round(wall, 3), "commands": len(steps),
                   "exit_codes": codes, "ok": ok, "log": str(log_path.relative_to(run))}
            out.write(json.dumps(rec) + "\n")
            out.flush()
            print(f"{'ok ' if ok else 'ERR'} {name:<24} {wall:8.1f}s  exit={codes}", flush=True)
    set_session(args.proxy, "")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

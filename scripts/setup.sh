#!/usr/bin/env bash
# Fetch pinned upstream checkouts into vendor/, apply the bench patches, and
# build everything. Safe to re-run: existing checkouts are kept as they are.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD

# repo-dir  url  branch  commit
PINS=(
  "local-pir-rpc https://github.com/kassandraoftroy/local-pir-rpc main a46ec937101ea449f7f7a475cc3a309b5fdaf889"
  "kohaku-rs https://github.com/ethereum/kohaku-rs experiments/pir-v1 ea2430916422165e62d15a80536df7029d9c1213"
  "inspire-gpu-serving https://github.com/keewoolee/inspire-gpu-serving main 3dd5c44e22cdc102a8549c67dff621240e0421b6"
  "kohaku-cli https://github.com/kassandraoftroy/kohaku-cli main e7d8e9d54661cbfe3134a7afa415910adad0e711"
)
NODE_MAJOR=22

mkdir -p vendor .tools
for pin in "${PINS[@]}"; do
  read -r dir url branch commit <<<"$pin"
  if [[ ! -d vendor/$dir/.git ]]; then
    echo "== clone $dir @ ${commit:0:10}"
    git clone -q --branch "$branch" "$url" "vendor/$dir"
    git -C "vendor/$dir" checkout -q "$commit"
    if [[ -f patches/$dir.patch && -s patches/$dir.patch ]]; then
      git -C "vendor/$dir" apply "$ROOT/patches/$dir.patch"
      echo "   applied patches/$dir.patch"
    fi
  else
    echo "== keep vendor/$dir ($(git -C "vendor/$dir" rev-parse --short HEAD), local changes kept)"
  fi
done
git -C vendor/inspire-gpu-serving submodule update --init --depth 1 -q

# Node 22+ for kohaku-cli, installed locally if the system node is older.
node_ok() { "$1" -e "process.exit(+process.versions.node.split('.')[0] >= $NODE_MAJOR ? 0 : 1)" 2>/dev/null; }
if [[ -x .tools/node/bin/node ]] && node_ok .tools/node/bin/node; then
  :
elif command -v node >/dev/null && node_ok node; then
  mkdir -p .tools/node/bin && ln -sf "$(command -v node)" .tools/node/bin/node
  ln -sf "$(dirname "$(command -v node)")/npm" .tools/node/bin/npm
else
  echo "== install node $NODE_MAJOR into .tools/"
  V=$(curl -fsSL https://nodejs.org/dist/index.json | python3 -c "import json,sys;print([r['version'] for r in json.load(sys.stdin) if r['version'].startswith('v$NODE_MAJOR.')][0])")
  curl -fsSL "https://nodejs.org/dist/$V/node-$V-linux-x64.tar.xz" | tar xJ -C .tools
  ln -sfn "node-$V-linux-x64" .tools/node
fi
export PATH=$ROOT/.tools/node/bin:$PATH
echo "== kohaku-cli (node $(node --version))"
(cd vendor/kohaku-cli && npm ci --silent --no-audit --no-fund && npm run -s build >/dev/null)

# Some hosts shadow `cc` with a non-compiler; fall back to gcc for cargo.
if ! cc --version >/dev/null 2>&1; then
  export CC=gcc CXX=g++ CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER=gcc
fi
echo "== cargo build (proxy, pir-bench)"
(cd vendor/local-pir-rpc && cargo build --release -q)
(cd pir-bench && cargo build --release -q)
echo "== done. Next: cp bench.env.example bench.env, edit ETH_RPC_URL, then make bench"

#!/usr/bin/env bash
# Copy each run's report.html into runs/_site/ (reports only, never wallet data)
# and serve that folder on the Tailscale address. Re-run after a new report.
#   scripts/serve-reports.sh          refresh + start server if not running
set -euo pipefail
cd "$(dirname "$0")/.."
SITE=runs/_site
PORT=${REPORT_PORT:-8790}
BIND=${REPORT_BIND:-$(tailscale ip -4 2>/dev/null | head -1 || echo 127.0.0.1)}
mkdir -p "$SITE"
links=""
for r in $(ls -1d runs/*/ 2>/dev/null | grep -v _site | sort -r); do
  id=$(basename "$r")
  [[ -f $r/report.html ]] || continue
  cp "$r/report.html" "$SITE/$id.html"
  links+="<li><a href=\"$id.html\">$id</a></li>"
done
cat > "$SITE/index.html" <<HTML
<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>PIR wallet bench runs</title>
<style>body{font:15px/1.6 system-ui,sans-serif;max-width:640px;margin:32px auto;padding:0 16px;background:#f9f9f7;color:#0b0b0b}
@media (prefers-color-scheme:dark){body{background:#0d0d0d;color:#fff}a{color:#86b6ef}}</style></head>
<body><h1>PIR wallet bench runs</h1><ul>$links</ul></body></html>
HTML
if ! curl -fs -m 1 "http://$BIND:$PORT/" 2>/dev/null | grep -q "PIR wallet bench runs"; then
  if ss -ltn | grep -qE ":$PORT\b"; then echo "port $PORT is taken by another server; set REPORT_PORT"; exit 1; fi
  setsid nohup python3 -m http.server "$PORT" --bind "$BIND" --directory "$SITE" >/dev/null 2>&1 < /dev/null &
  sleep 1
fi
echo "tailnet: http://$BIND:$PORT/"

# Public copy: same pages with the PIR server address replaced, served on
# localhost only. Tailscale Funnel (port 10000, nothing else on it) is the only
# way in from outside. Enable once with:
#   tailscale funnel --bg --https=10000 http://127.0.0.1:$PUBLIC_PORT
PUB=runs/_public
PUBLIC_PORT=${REPORT_PUBLIC_PORT:-8791}
mkdir -p "$PUB"
rm -f "$PUB"/*.html
for f in "$SITE"/*.html; do
  sed -E 's#https?://[0-9]{1,3}(\.[0-9]{1,3}){3}(:[0-9]+)?#EF PIR server (Netherlands)#g; s#[0-9]{1,3}(\.[0-9]{1,3}){3}#[redacted]#g' "$f" > "$PUB/$(basename "$f")"
done
if grep -qE '[0-9]{1,3}(\.[0-9]{1,3}){3}' "$PUB"/*.html; then echo "public copy still holds an IP; not serving"; exit 1; fi
if ! curl -fs -m 1 "http://127.0.0.1:$PUBLIC_PORT/" 2>/dev/null | grep -q "PIR wallet bench runs"; then
  if ss -ltn | grep -qE "127.0.0.1:$PUBLIC_PORT\b|:$PUBLIC_PORT\b"; then echo "port $PUBLIC_PORT is taken; set REPORT_PUBLIC_PORT"; exit 1; fi
  setsid nohup python3 -m http.server "$PUBLIC_PORT" --bind 127.0.0.1 --directory "$PUB" >/dev/null 2>&1 < /dev/null &
  sleep 1
fi
if [[ $(tailscale funnel status 2>/dev/null) == *":10000"* ]]; then
  echo "public:  https://$(tailscale status --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))'):10000/"
fi

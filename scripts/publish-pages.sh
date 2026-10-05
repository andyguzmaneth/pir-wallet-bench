#!/usr/bin/env bash
# Publish the redacted report copies (runs/_public/) to the gh-pages branch.
# GitHub Pages serves them at https://<owner>.github.io/<repo>/.
# Run scripts/serve-reports.sh first; it builds runs/_public/.
set -euo pipefail
cd "$(dirname "$0")/.."
PUB=runs/_public
[[ -f $PUB/index.html ]] || { echo "no $PUB/index.html; run scripts/serve-reports.sh"; exit 1; }
if grep -qE '[0-9]{1,3}(\.[0-9]{1,3}){3}' "$PUB"/*.html; then echo "public copy holds an IP; not publishing"; exit 1; fi
W=$(mktemp -d)
trap 'git worktree remove --force "$W" >/dev/null 2>&1 || true; rm -rf "$W"' EXIT
git fetch -q origin gh-pages
git worktree add -q "$W" origin/gh-pages 2>/dev/null || git worktree add -q --detach "$W" origin/gh-pages
cp "$PUB"/*.html "$W"/
touch "$W/.nojekyll"
cd "$W"
git add -A
if git diff --cached --quiet; then echo "pages: nothing new"; exit 0; fi
git commit -qm "pages: update reports"
git push -q origin HEAD:gh-pages
url=$(gh api "repos/$(gh repo view --json nameWithOwner -q .nameWithOwner)/pages" --jq .html_url 2>/dev/null || true)
echo "pages: ${url:-pushed to gh-pages}"

#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"

if ! command -v asciinema >/dev/null 2>&1; then
  echo "asciinema is required" >&2
  exit 1
fi
if ! command -v agg >/dev/null 2>&1; then
  echo "agg is required" >&2
  exit 1
fi

work=$(mktemp -d)
script=$(mktemp)
cleanup() {
  rm -rf "$work"
  rm -f "$script"
}
trap cleanup EXIT

cp -a examples/sample-api/. "$work/"
export DEMO_ROOT="$root"
export DEMO_WORK="$work"

cat >"$script" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
unset ANTHROPIC_API_KEY
cd "$DEMO_WORK"

pause() {
  sleep "${1:-0.7}"
}

show() {
  printf '\n$ %s\n' "$1"
  pause 0.5
}

run() {
  uv run --project "$DEMO_ROOT" mcp4api "$@"
}

show "mcp4api ingest openapi.yaml --offline --deterministic --out ."
run ingest openapi.yaml --offline --deterministic --out .
pause 0.8

show "mcp4api curate --offline --deterministic --out ."
run curate --offline --deterministic --out .
pause 0.6

show "grep -E '^- name:' tools.yaml"
grep -E '^- name:' tools.yaml
pause 1

show "mcp4api verify --offline --deterministic --out ."
set +e
run verify --offline --deterministic --out .
code=$?
set -e
if [ "$code" -ne 3 ]; then
  echo "expected exit 3, got $code" >&2
  exit 1
fi
printf 'exit %s\n' "$code"
pause 1.2

show "sed -n '1,11p;33,46p' report/verification.md"
sed -n '1,11p;33,46p' report/verification.md
pause 2
EOF
chmod +x "$script"

asciinema rec --overwrite --window-size 100x32 --idle-time-limit 2 --return -c "$script" examples/demo.cast
agg --speed 1 --font-size 13 --idle-time-limit 2 --last-frame-duration 3 examples/demo.cast examples/demo.gif

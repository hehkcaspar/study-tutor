#!/bin/bash
# Tutor watcher: scans a topic's annotations and answers anything "open" via
# headless `claude -p` (tutor_answer.py). Runs until Ctrl-C / window close.
#
#   bash tutor_watch.sh [TOPIC_DIR] [initial-interval]
#
# Scan interval: 5-30s (default 10), stored in <topic>/settings.json and
# re-read every loop, so changes from the reader UI apply live.
ENGINE="$(cd "$(dirname "$0")" && pwd)"
TOPIC="${1:-$PWD}"
TOPIC="$(cd "$TOPIC" 2>/dev/null && pwd)" || { echo "no such topic dir: $1"; exit 1; }

if [ -n "$2" ]; then
  python3 - "$2" "$TOPIC" <<'EOF' || echo "invalid interval '$2', ignoring"
import json, sys
v = min(30, max(5, int(sys.argv[1])))
p = sys.argv[2] + "/settings.json"
try: s = json.load(open(p))
except Exception: s = {}
s["scanInterval"] = v
json.dump(s, open(p, "w"), indent=2)
print(f"scan interval set to {v}s")
EOF
fi

current_interval() {
  python3 - "$TOPIC" <<'EOF' 2>/dev/null || echo 10
import json, sys
try: v = int(json.load(open(sys.argv[1] + "/settings.json")).get("scanInterval", 10))
except Exception: v = 10
print(min(30, max(5, v)))
EOF
}

echo "🤖 tutor watcher [$TOPIC]: scanning every $(current_interval)s — open questions answered by claude -p"
while true; do
  python3 "$ENGINE/tutor_answer.py" "$TOPIC"
  sleep "$(current_interval)"
done

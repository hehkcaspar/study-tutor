#!/bin/bash
# Study-tutor: reader server + tutor watcher for one topic directory.
# Keep this terminal window open while studying; closing it stops both.
#
#   bash <skill>/engine/start.sh [TOPIC_DIR] [scan-interval 5-30]
#
# TOPIC_DIR defaults to the current directory. Each topic has its own port
# (settings.json "port"), so several topics can run side by side.
ENGINE="$(cd "$(dirname "$0")" && pwd)"
TOPIC="${1:-$PWD}"
TOPIC="$(cd "$TOPIC" 2>/dev/null && pwd)" || { echo "no such topic dir: $1"; exit 1; }
if [ ! -f "$TOPIC/settings.json" ]; then
  echo "No settings.json in $TOPIC — initialize it first:"
  echo "  bash $ENGINE/init.sh \"$TOPIC\" \"My Topic Title\""
  exit 1
fi

PORT=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('port', 8787))" "$TOPIC/settings.json" 2>/dev/null || echo 8787)

# free OUR port only (a stale instance of this same topic), then start fresh
lsof -ti "tcp:$PORT" | xargs kill 2>/dev/null
sleep 0.3
python3 "$ENGINE/serve.py" --dir "$TOPIC" "$PORT" &
SERVER=$!
trap 'kill $SERVER 2>/dev/null; echo "👋 study session ended — if this lesson used cloud resources (GPU VMs etc.), make sure they are DELETED, not just stopped."' EXIT
sleep 0.5
URL="http://127.0.0.1:$PORT"
echo "📚 reader: $URL   topic: $TOPIC"
open "$URL" 2>/dev/null || xdg-open "$URL" 2>/dev/null || echo "   (open $URL in your browser)"

bash "$ENGINE/tutor_watch.sh" "$TOPIC" "$2"   # foreground — keeps the window alive

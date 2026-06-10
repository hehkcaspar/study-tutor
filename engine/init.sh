#!/bin/bash
# Scaffold a new study topic.
#
#   bash ~/.study-tutor/init.sh TOPIC_DIR ["Topic Title"]
#
# Creates TOPIC_DIR with settings.json (unique port from the registry),
# annotations.json, and a curriculum.md template. Safe to re-run: existing
# files are never overwritten.
ENGINE="$(cd "$(dirname "$0")" && pwd)"
TOPIC_DIR="$1"
[ -n "$TOPIC_DIR" ] || { echo "usage: init.sh TOPIC_DIR [\"Topic Title\"]"; exit 1; }
mkdir -p "$TOPIC_DIR" || exit 1
TOPIC_DIR="$(cd "$TOPIC_DIR" && pwd)"
TITLE="${2:-$(basename "$TOPIC_DIR") — Study Notes}"

python3 - "$ENGINE" "$TOPIC_DIR" "$TITLE" <<'EOF'
import json, sys
from pathlib import Path

engine, topic_dir, title = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]

# pick a unique port via the engine's topic registry
reg_path = engine / "topics.json"
try:
    reg = json.loads(reg_path.read_text())
except Exception:
    reg = {}
if str(topic_dir) in reg:
    port = reg[str(topic_dir)]
else:
    used = set(reg.values())
    port = next(p for p in range(8787, 8987) if p not in used)
    reg[str(topic_dir)] = port
    reg_path.write_text(json.dumps(reg, indent=2))

s_path = topic_dir / "settings.json"
if s_path.exists():
    print(f"settings.json already exists (port {json.loads(s_path.read_text()).get('port', '?')}) — leaving it alone")
else:
    s_path.write_text(json.dumps({
        "topic": topic_dir.name,
        "title": title,
        "learner": "",
        "learnerProfile": "",
        "port": port,
        "scanInterval": 10,
    }, indent=2))
    print(f"settings.json created (port {port}) — fill in learner/learnerProfile for a personalized tutor")

a_path = topic_dir / "annotations.json"
if not a_path.exists():
    a_path.write_text("[]")

c_path = topic_dir / "curriculum.md"
if not c_path.exists():
    c_path.write_text(f"""# {title} — Curriculum & Learner Profile

**Last updated:** (date)

## Learner

- (who you are, background, what you already know)
- **Goal:** (what you want to be able to do)
- **Learning style:** (how you learn best — analogies? math first? hands-on?)

## Curriculum plan

| # | Lesson | Status | Notes file |
|---|---|---|---|
| 1 | (first lesson) | planned | — |

## Observations for personalization (tutor's log)

- (the tutor appends what it learns about how you learn)

## Open threads

- (pending questions, next steps)
""")
    print("curriculum.md template created — edit the Learner section before lesson 1")

print(f"\nStart studying:  bash {engine}/start.sh {topic_dir}")
EOF

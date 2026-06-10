#!/usr/bin/env python3
"""One scan pass: answer open annotations via headless `claude -p`.

Usage: python3 tutor_answer.py [TOPIC_DIR]      (default: cwd)

All writes go through the engine server's HTTP API (single-writer store) with
an ifStatus precondition, so a user resolving/deleting a question mid-
generation gets a 409 instead of being overwritten. Per-annotation exponential
backoff lives in <topic>/.tutor_state.json so persistent failures don't retry
every cycle. Persona comes from settings.json (topic/learner/learnerProfile)
plus curriculum.md as tutoring context.
"""
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

MAX_NOTE_CHARS = 9000
MAX_PERSONA_CHARS = 3000
BACKOFF_BASE, BACKOFF_MAX = 20, 600  # seconds


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def build_prompt(ann, note_text, settings, curriculum):
    learner = settings.get("learner") or "the learner"
    topic = settings.get("topic") or "this subject"
    profile = settings.get("learnerProfile") or ""
    convo = ""
    if ann.get("comment"):
        convo += f"\nLearner's question: {ann['comment']}"
    elif not ann.get("thread"):
        convo += "\nLearner flagged this passage as \"don't get it yet\" — explain it."
    if ann.get("answer"):
        convo += f"\n\nTutor (you), earlier: {ann['answer']}"
    for m in ann.get("thread", []):
        who = "Tutor (you)" if m["role"] == "tutor" else "Learner"
        convo += f"\n\n{who}: {m['text']}"
    profile_line = f" Learner profile: {profile}." if profile else ""
    return f"""You are {learner}'s personal tutor for {topic}.{profile_line} Be concise, no fluff.

Tutor context — curriculum & personalization notes:
---
{curriculum[:MAX_PERSONA_CHARS]}
---

Study note `{ann['file']}` (full text):
---
{note_text[:MAX_NOTE_CHARS]}
---

The learner highlighted this passage: "{ann['text']}"
(surrounding context: …{ann.get('prefix', '')}⟦HIGHLIGHT⟧{ann.get('suffix', '')}…)
{convo}

Write the next tutor reply: concise, human-learning-friendly markdown, <= 200 words.
The reader renders KaTeX math ($...$ inline, $$...$$ display) and ```mermaid diagrams.
HARD RULE: every formula and every math symbol MUST be written as $...$/$$...$$ LaTeX.
Backticks are ONLY for code identifiers like config keys or filenames — NEVER for math.
Add a small mermaid diagram when structure/flow helps.
Output ONLY the reply markdown — no preamble, no sign-off."""


def main():
    topic_dir = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    settings = read_json(topic_dir / "settings.json", {})
    port = int(settings.get("port", 8787))
    base = f"http://127.0.0.1:{port}"

    def api(path, payload=None):
        req = urllib.request.Request(
            base + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json"},
            method="POST" if payload is not None else "GET")
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())

    try:
        anns = api("/api/annotations")
    except (urllib.error.URLError, OSError) as e:
        print(f"[{time.strftime('%H:%M:%S')}] server unreachable on :{port} ({e}); will retry", flush=True)
        return
    open_anns = [a for a in anns if a.get("status") == "open"]
    if not open_anns:
        return

    if not shutil.which("claude"):
        print("claude CLI not found on PATH — cannot answer; install/login first", flush=True)
        return

    state_path = topic_dir / ".tutor_state.json"
    state = read_json(state_path, {})
    state = {k: v for k, v in state.items() if k in {a["id"] for a in open_anns}}  # prune
    now = time.time()
    curriculum = ""
    cur = topic_dir / "curriculum.md"
    if cur.exists():
        curriculum = cur.read_text(encoding="utf-8")

    for ann in open_anns:
        aid = ann["id"]
        ent = state.get(aid, {"fails": 0, "next": 0})
        if now < ent.get("next", 0):
            continue  # backing off after earlier failures
        note_path = topic_dir / Path(ann["file"]).name
        note_text = note_path.read_text(encoding="utf-8") if note_path.exists() else ""
        print(f"[{time.strftime('%H:%M:%S')}] answering {aid} ({ann['type']}) on {ann['file']} …", flush=True)
        try:
            out = subprocess.run(
                ["claude", "-p", "--output-format", "text"],
                input=build_prompt(ann, note_text, settings, curriculum),
                capture_output=True, text=True, timeout=240,
            )
            answer = out.stdout.strip()
            ok = out.returncode == 0 and bool(answer)
            err = out.stderr.strip()[:200]
        except Exception as e:
            ok, answer, err = False, "", str(e)
        if not ok:
            ent["fails"] += 1
            ent["next"] = time.time() + min(BACKOFF_MAX, BACKOFF_BASE * 2 ** ent["fails"])
            state[aid] = ent
            Path(state_path).write_text(json.dumps(state, indent=2), encoding="utf-8")
            print(f"  ✗ claude -p failed (attempt {ent['fails']}, backing off "
                  f"{int(ent['next'] - time.time())}s): {err}", flush=True)
            continue
        # write through the API with a precondition: only if still open
        payload = {"ifStatus": "open"}
        if not ann.get("answer"):
            payload.update({"answer": answer, "status": "answered"})
        else:
            payload["reply"] = {"role": "tutor", "text": answer}
        try:
            api(f"/api/annotations/{aid}", payload)
            print(f"  ✓ answered {aid}", flush=True)
        except urllib.error.HTTPError as e:
            if e.code == 409:
                print(f"  ⚠ {aid} changed while generating (resolved/deleted) — answer discarded", flush=True)
            else:
                print(f"  ✗ write failed for {aid}: HTTP {e.code}", flush=True)
                continue
        except (urllib.error.URLError, OSError) as e:
            print(f"  ✗ write failed for {aid}: {e}", flush=True)
            continue
        state.pop(aid, None)
        Path(state_path).write_text(json.dumps(state, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

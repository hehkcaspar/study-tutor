#!/usr/bin/env python3
"""Study-tutor engine server (stdlib only).

One shared engine, many topic directories. A topic dir holds only content:
*.md notes, annotations.json, settings.json. The app shell and assets are
served from the engine dir; everything else from the topic dir.

Usage:  python3 serve.py --dir TOPIC_DIR [port]
        (port: CLI arg > settings.json "port" > 8787)

API:
  GET  /api/config                -> topic settings (title, topic, learner, ...)
  GET  /api/status                -> {annotations: etag, notes: etag, scanInterval}
  GET  /api/notes                 -> [{file, title, mtime, open, answered}]
  GET  /api/annotations           -> all annotations
  POST /api/annotations           -> create (file must be an existing .md)
  POST /api/annotations/<id>      -> update {status|comment|answer|reply|delete};
                                     optional "ifStatus": precondition, 409 on mismatch
  POST /api/settings              -> merge settings (scanInterval clamped 5-30)
  POST /api/translate             -> {file} -> Simplified-Chinese markdown of that
                                     note via `claude -p`; cached in .translations.json
                                     keyed by source hash (auto-invalidates on edit)

Concurrency: this server is the single writer of annotations.json/settings.json
(the tutor writes through this API). Mutations hold a process-wide lock and
writes are atomic (tmp + os.replace), so readers never see torn files.
"""
import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ENGINE = Path(__file__).parent.resolve()
TOPIC = Path.cwd()  # overridden in main()
LOCK = threading.Lock()
SCAN_MIN, SCAN_MAX, SCAN_DEFAULT = 5, 30, 10
_title_cache = {}


def ann_path():
    return TOPIC / "annotations.json"


def settings_path():
    return TOPIC / "settings.json"


def trans_path():
    return TOPIC / ".translations.json"


TRANSLATE_PROMPT = """Translate the following markdown study note into Simplified Chinese.
PRESERVE EXACTLY, unchanged: markdown structure and heading levels, table layout
(translate only the prose inside cells), fenced code blocks and inline code,
LaTeX math ($...$ and $$...$$), mermaid blocks, URLs, file paths, and emoji.
Keep standard ML/technical terms in English (e.g. LoRA, SFT, GRPO, KL, logits,
checkpoint), adding a Chinese gloss in parentheses on first occurrence when helpful.
Output ONLY the translated markdown — no preamble, no fences around the whole document.

--- NOTE ---
"""


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def load_anns():
    return read_json(ann_path(), [])


def load_settings():
    s = read_json(settings_path(), {})
    try:
        v = int(s.get("scanInterval", SCAN_DEFAULT))
    except (TypeError, ValueError):
        v = SCAN_DEFAULT
    s["scanInterval"] = min(SCAN_MAX, max(SCAN_MIN, v))
    return s


def note_title(p):
    st = p.stat().st_mtime_ns
    hit = _title_cache.get(p.name)
    if hit and hit[0] == st:
        return hit[1]
    m = re.search(r"^# (.+)$", p.read_text(encoding="utf-8"), re.M)
    title = m.group(1).strip() if m else p.stem
    _title_cache[p.name] = (st, title)
    return title


def anns_etag():
    try:
        st = ann_path().stat()
        return f"{st.st_mtime_ns}-{st.st_size}"
    except FileNotFoundError:
        return "0"


def notes_etag():
    parts = sorted(f"{p.name}:{p.stat().st_mtime_ns}" for p in TOPIC.glob("*.md"))
    return hashlib.md5("|".join(parts).encode()).hexdigest()[:12]


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(TOPIC), **kwargs)

    def translate_path(self, path):
        clean = path.split("?", 1)[0].split("#", 1)[0]
        if clean in ("/", "/app.html", "/index.html"):
            return str(ENGINE / "app.html")
        if clean.startswith("/assets/"):
            rel = posixpath.normpath(clean.lstrip("/"))
            if rel.startswith("assets/"):
                return str(ENGINE / rel)
            return str(ENGINE / "nonexistent")
        return super().translate_path(path)

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        try:
            self.handle_get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)
            except Exception:
                pass

    def do_POST(self):
        try:
            self.handle_post()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)
            except Exception:
                pass

    def handle_get(self):
        route = self.path.split("?", 1)[0]
        if route == "/api/config":
            return self.send_json(load_settings())
        if route == "/api/status":
            return self.send_json({
                "annotations": anns_etag(),
                "notes": notes_etag(),
                "scanInterval": load_settings()["scanInterval"],
            })
        if route == "/api/notes":
            anns = load_anns()
            notes = []
            for p in sorted(TOPIC.glob("*.md")):
                notes.append({
                    "file": p.name,
                    "title": note_title(p),
                    "mtime": p.stat().st_mtime_ns,
                    "open": sum(1 for a in anns
                                if a["file"] == p.name and a["status"] == "open"),
                    "answered": sum(1 for a in anns
                                    if a["file"] == p.name and a["status"] == "answered"),
                })
            return self.send_json(notes)
        if route == "/api/annotations":
            return self.send_json(load_anns())
        return super().do_GET()

    def handle_post(self):
        route = self.path.split("?", 1)[0]
        if route == "/api/translate":
            b = self.read_body()
            fname = Path(b.get("file") or "").name
            p = TOPIC / fname
            if not fname.endswith(".md") or not p.exists():
                return self.send_json({"error": f"unknown note: {fname!r}"}, 400)
            src = p.read_text(encoding="utf-8")
            h = hashlib.md5(src.encode()).hexdigest()[:16]
            ent = read_json(trans_path(), {}).get(fname)
            if ent and ent.get("hash") == h:
                return self.send_json({"file": fname, "markdown": ent["zh"], "cached": True})
            if not shutil.which("claude"):
                return self.send_json({"error": "claude CLI not on PATH — cannot translate"}, 503)
            out = subprocess.run(["claude", "-p", "--output-format", "text"],
                                 input=TRANSLATE_PROMPT + src,
                                 capture_output=True, text=True, timeout=300)
            zh = out.stdout.strip()
            if out.returncode != 0 or not zh:
                return self.send_json(
                    {"error": f"translation failed: {out.stderr.strip()[:200]}"}, 502)
            with LOCK:
                cache = read_json(trans_path(), {})
                cache[fname] = {"hash": h, "zh": zh, "at": now_str()}
                write_json(trans_path(), cache)
            return self.send_json({"file": fname, "markdown": zh, "cached": False})
        if route == "/api/settings":
            b = self.read_body()
            with LOCK:
                s = load_settings()
                for k, v in b.items():
                    s[k] = v
                try:
                    s["scanInterval"] = min(SCAN_MAX, max(SCAN_MIN, int(s.get("scanInterval", SCAN_DEFAULT))))
                except (TypeError, ValueError):
                    s["scanInterval"] = SCAN_DEFAULT
                write_json(settings_path(), s)
            return self.send_json(s)

        if route == "/api/annotations":
            b = self.read_body()
            fname = Path(b.get("file") or "").name
            if not fname.endswith(".md") or not (TOPIC / fname).exists():
                return self.send_json({"error": f"unknown note: {fname!r}"}, 400)
            ann = {
                "id": uuid.uuid4().hex[:8],
                "file": fname,
                "text": b.get("text", ""),
                "prefix": b.get("prefix", ""),
                "suffix": b.get("suffix", ""),
                "occurrence": int(b.get("occurrence", 0)),
                "type": "question" if b.get("type") == "question" else "flag",
                "comment": (b.get("comment") or "").strip(),
                "status": "open",
                "answer": "",
                "created": now_str(),
            }
            with LOCK:
                anns = load_anns()
                anns.append(ann)
                write_json(ann_path(), anns)
            return self.send_json(ann, 201)

        m = re.match(r"^/api/annotations/([0-9a-f]+)$", route)
        if m:
            patch = self.read_body()
            with LOCK:
                anns = load_anns()
                for ann in anns:
                    if ann["id"] != m.group(1):
                        continue
                    if "ifStatus" in patch and ann["status"] != patch["ifStatus"]:
                        return self.send_json(
                            {"error": "precondition failed", "status": ann["status"]}, 409)
                    if patch.get("delete"):
                        anns.remove(ann)
                        write_json(ann_path(), anns)
                        return self.send_json({"ok": True})
                    if "reply" in patch:
                        r = patch["reply"] or {}
                        msg = {
                            "role": "tutor" if r.get("role") == "tutor" else "learner",
                            "text": (r.get("text") or "").strip(),
                            "at": now_str(),
                        }
                        if msg["text"]:
                            ann.setdefault("thread", []).append(msg)
                            # learner follow-up reopens; tutor reply marks answered
                            ann["status"] = "answered" if msg["role"] == "tutor" else "open"
                    for k in ("status", "comment", "answer"):
                        if k in patch:
                            ann[k] = patch[k]
                    if ann["status"] == "answered" and (patch.get("status") == "answered"
                            or (patch.get("reply") or {}).get("role") == "tutor"):
                        ann["answeredAt"] = now_str()
                    write_json(ann_path(), anns)
                    return self.send_json(ann)
            return self.send_json({"error": "not found"}, 404)
        return self.send_json({"error": "bad path"}, 404)

    def log_message(self, fmt, *args):
        pass


def main():
    global TOPIC
    args = sys.argv[1:]
    port = None
    if "--dir" in args:
        i = args.index("--dir")
        TOPIC = Path(args[i + 1]).resolve()
        args = args[:i] + args[i + 2:]
    if args:
        port = int(args[0])
    if not TOPIC.is_dir():
        sys.exit(f"topic dir not found: {TOPIC}")
    if port is None:
        port = int(load_settings().get("port", 8787))
    print(f"study-tutor: topic={TOPIC}  http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()

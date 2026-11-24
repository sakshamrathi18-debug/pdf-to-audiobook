"""Local web UI for make_audiobook.py. Run: .venv/bin/python server.py  ->  http://localhost:8765"""
import json, mimetypes, queue, re, shutil, threading, traceback
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

import make_audiobook as mk

HERE = Path(__file__).parent
UI, UPLOADS, VOICES, OUT = HERE / "ui", HERE / "uploads", HERE / "voices", HERE / "out"
for d in (UPLOADS, VOICES, OUT):
    d.mkdir(exist_ok=True)

books = {}          # name -> {"chapters": [...], "m4b": str|None, "error": str|None, "preparing": bool}
jobs = queue.Queue()
current = {"book": None, "chapter": None, "select": []}
stop_flag = threading.Event()
lock = threading.Lock()


def book(name):
    return books.setdefault(name, {"chapters": [], "m4b": None, "error": None, "preparing": True})


def make_emit(name):
    def emit(e):
        with lock:
            b = book(name)
            t = e["type"]
            if t == "chapters":
                b["chapters"] = [{**c, "progress": c["have"] / max(c["chunks"], 1)} for c in e["chapters"]]
                b["preparing"] = False
                f = OUT / name / f"{name}.m4b"
                b["m4b"] = f.name if f.exists() else None
            elif t == "chunk":
                current["chapter"] = e["n"]
                c = b["chapters"][e["n"] - 1]
                c["progress"] = e["done"] / max(e["total"], 1)
            elif t == "chapter_done":
                c = b["chapters"][e["n"] - 1]
                c.update(done=True, progress=1, file=e["file"])
            elif t == "finished":
                b["m4b"] = e["m4b"] or b["m4b"]
    return emit


def worker():
    while True:
        job = jobs.get()
        name = job["name"]
        stop_flag.clear()
        current.update(book=name, chapter=None, select=sorted(job["select"]))
        try:
            mk.run_job(UPLOADS / f"{name}.pdf", job.get("voice"), job["emotion"], select=job["select"],
                       emit=make_emit(name), stop=stop_flag.is_set)
            book(name)["error"] = None
        except Exception as e:  # shown in the UI; the worker keeps going
            traceback.print_exc()
            with lock:
                b = book(name)
                b["error"], b["preparing"] = str(e), False
        current.update(book=None, chapter=None, select=[])


def with_files(name):
    """Attach the file name of every finished chapter (also those made by the command line)."""
    b = books[name]
    for c in b["chapters"]:
        f = mk.chapter_file(OUT / name, c["n"] - 1, c["title"])
        c["done"] = f.exists()
        if c["done"]:
            c["file"] = f.name
            c["progress"] = 1
    return b


def state():
    with lock:
        return {"books": {n: with_files(n) for n in books}, "current": dict(current),
                "voices": sorted(p.name for p in VOICES.glob("*.wav")), "queued": jobs.qsize()}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path):
        if not path.is_file():
            return self.send_json({"error": "not found"}, 404)
        size, start, end, code = path.stat().st_size, 0, None, 200
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m and (m[1] or m[2]):  # audio seeking needs Range support
            start = int(m[1]) if m[1] else max(size - int(m[2]), 0)
            end = int(m[2]) if m[1] and m[2] else size - 1
            code = 206
        end = size - 1 if end is None else min(end, size - 1)
        self.send_response(code)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                buf = f.read(min(1 << 16, left))
                if not buf:
                    break
                try:
                    self.wfile.write(buf)
                except BrokenPipeError:
                    return
                left -= len(buf)

    def do_GET(self):
        u = urlparse(self.path)
        p = unquote(u.path)
        if p == "/api/state":
            return self.send_json(state())
        if p.startswith("/audio/"):
            parts = p[7:].split("/")
            if len(parts) == 2 and ".." not in p:
                return self.send_file(OUT / parts[0] / parts[1])
            return self.send_json({"error": "bad path"}, 400)
        f = UI / ("index.html" if p == "/" else p.lstrip("/"))
        if UI in f.resolve().parents:
            return self.send_file(f)
        self.send_json({"error": "not found"}, 404)

    def body(self):
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def do_POST(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/api/upload":  # raw PDF bytes, ?name=file.pdf
            stem = re.sub(r"[^\w .,'()&-]", "", Path(q.get("name", "book.pdf")).stem).strip() or "book"
            (UPLOADS / f"{stem}.pdf").write_bytes(self.body())
            book(stem).update(preparing=True, error=None)
            jobs.put({"name": stem, "select": set(), "emotion": 0.6})  # empty selection = just read chapters
            return self.send_json({"name": stem})
        if u.path == "/api/voice":
            name = re.sub(r"[^\w .-]", "", Path(q.get("name", "voice.wav")).name)
            (VOICES / name).write_bytes(self.body())
            return self.send_json({"name": name})
        if u.path == "/api/start":
            d = json.loads(self.body())
            voice = VOICES / d["voice"] if d.get("voice") else None
            if d["book"] not in books or (voice and not voice.is_file()):
                return self.send_json({"error": "unknown book or voice"}, 400)
            jobs.put({"name": d["book"], "select": {int(n) for n in d["chapters"]},
                      "emotion": float(d.get("emotion", 0.6)), "voice": str(voice) if voice else None})
            return self.send_json({"ok": True})
        if u.path == "/api/stop":
            stop_flag.set()
            while not jobs.empty():
                try:
                    jobs.get_nowait()
                except queue.Empty:
                    break
            return self.send_json({"ok": True})
        self.send_json({"error": "not found"}, 404)


if __name__ == "__main__":
    for pdf in sorted(UPLOADS.glob("*.pdf")):  # books from earlier sessions come back
        book(pdf.stem)
        jobs.put({"name": pdf.stem, "select": set(), "emotion": 0.6})
    threading.Thread(target=worker, daemon=True).start()
    print("Audiobook maker on http://localhost:8765")
    ThreadingHTTPServer(("127.0.0.1", 8765), H).serve_forever()

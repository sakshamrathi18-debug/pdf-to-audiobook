"""PDF -> chaptered .m4b audiobook, fully local: PyMuPDF + Ollama (chapters) + Chatterbox (voice)."""
import argparse, collections, json, re, subprocess, sys, time, urllib.request, wave
from pathlib import Path

HERE = Path(__file__).parent
SR = 24000
CHUNK_CHARS = 300
PAUSE_CHUNK, PAUSE_PARA, PAUSE_CHAPTER = 0.25, 1.2, 2.0


# ---------- 1. extract ----------
def extract(pdf, max_pages=None):
    """Return paragraphs: [{page, text, size, bold}] with headers/footers/page numbers removed."""
    import pymupdf as fitz
    doc = fitz.open(pdf)
    pages = range(min(len(doc), max_pages or len(doc)))
    raw = []
    for p in pages:
        for b in doc[p].get_text("dict")["blocks"]:
            lines, sizes, bold = [], [], True
            for ln in b.get("lines", []):
                t = "".join(s["text"] for s in ln["spans"]).strip()
                if not t:
                    continue
                lines.append(t)
                for s in ln["spans"]:
                    if s["text"].strip():
                        sizes.append((round(s["size"], 1), len(s["text"])))
                        bold &= bool(s["flags"] & 16) or "bold" in s["font"].lower()
            if lines:
                size = max(sizes, key=lambda x: x[1])[0]
                raw.append({"page": p, "lines": lines, "size": size, "bold": bold})

    # repeated header/footer lines: same text (digits ignored) on many pages
    norm = lambda t: re.sub(r"\d+", "#", t.lower())
    seen = collections.defaultdict(set)
    for r in raw:
        for t in r["lines"]:
            if len(t) < 100:
                seen[norm(t)].add(r["page"])
    limit = max(3, 0.3 * len(pages))
    junk = {k for k, v in seen.items() if len(v) >= limit}
    pagenum = re.compile(r"^(page\s*)?(\d+|[ivxlcdm]+)$", re.I)

    paras = []
    for r in raw:
        kept = [t for t in r["lines"] if norm(t) not in junk and not pagenum.match(t)]
        if not kept:
            continue
        text = kept[0]
        for t in kept[1:]:
            text = text[:-1] + t if text.endswith("-") and t[:1].islower() else text + " " + t
        text = re.sub(r"\s+", " ", text).strip()
        prev = paras[-1] if paras else None
        # paragraph split by a page break: previous has no end punctuation and this starts lowercase
        if prev and prev["page"] != r["page"] and not re.search(r"[.!?:\"”’)]$", prev["text"]) and text[:1].islower():
            prev["text"] = prev["text"][:-1] + text if prev["text"].endswith("-") else prev["text"] + " " + text
            continue
        paras.append({"page": r["page"], "text": text, "size": r["size"], "bold": r["bold"]})
    return doc, paras


# ---------- 2. chapters ----------
FRONT = re.compile(r"title page|copyright|contents|dedication|epigraph|acknowledg|about the author|"
                   r"also by|praise|index|bibliograph|references|permissions|colophon|newsletter|excerpt", re.I)


def chapters_from_toc(doc, paras):
    toc = doc.get_toc()
    if not toc:
        return None
    top = min(e[0] for e in toc)
    out = []
    for _, title, page in (e for e in toc if e[0] == top):
        idx = [i for i, p in enumerate(paras) if p["page"] >= page - 1]
        if not idx:
            continue
        on_page = [i for i in idx if paras[i]["page"] == page - 1]
        key = re.sub(r"\W", "", title.lower())[:30]
        hit = next((i for i in on_page if key and key in re.sub(r"\W", "", paras[i]["text"].lower())), idx[0])
        out.append({"title": title.strip(), "start": hit, "skip": bool(FRONT.search(title))})
    return out or None


def heading_candidates(paras):
    weights = collections.Counter()
    for p in paras:
        weights[p["size"]] += len(p["text"])
    body = weights.most_common(1)[0][0]
    cands = [{"id": i, "page": p["page"] + 1, "text": p["text"], "big": round(p["size"] / body, 2)}
             for i, p in enumerate(paras)
             if len(p["text"]) < 80 and (p["size"] > body * 1.15 or p["bold"])]
    if len(cands) > 300:  # keep the biggest-font ones, then restore reading order
        cands = sorted(sorted(cands, key=lambda c: -c["big"])[:300], key=lambda c: c["id"])
    return cands


def validate_llm(reply, cands):
    """Keep only ids that were offered, in reading order, no duplicates."""
    ok = {c["id"]: c for c in cands}
    out, last = [], -1
    for ch in reply.get("chapters", []) if isinstance(reply, dict) else []:
        i = ch.get("id") if isinstance(ch, dict) else None
        if isinstance(i, int) and i in ok and i > last:
            out.append({"title": str(ch.get("title") or ok[i]["text"]).strip(), "start": i, "skip": bool(ch.get("skip"))})
            last = i
    return out


def chapters_from_llm(paras, model):
    cands = heading_candidates(paras)
    if not cands:
        return None
    listing = "\n".join(f'{c["id"]} | p{c["page"]} | size x{c["big"]} | {c["text"]}' for c in cands)
    prompt = (
        "These are possible headings found in a book PDF (id | page | font size vs body text | text).\n"
        "Pick the ones that start a chapter (or top-level part/section a listener would want as a chapter marker). "
        "Ignore running headers, figure captions, sub-headings inside a chapter, and table-of-contents entries.\n"
        "Give each a clean title. Set skip=true for things not worth narrating: copyright page, table of contents, "
        "index, bibliography/references, list of figures. Keep preface/introduction/epilogue (skip=false).\n\n" + listing)
    schema = {"type": "object", "properties": {"chapters": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "integer"}, "title": {"type": "string"}, "skip": {"type": "boolean"}},
        "required": ["id", "title", "skip"]}}}, "required": ["chapters"]}
    body = {"model": model, "stream": False, "think": False, "keep_alive": 0, "format": schema,
            "options": {"temperature": 0, "num_ctx": 16384},
            "messages": [{"role": "user", "content": prompt}]}
    try:
        req = urllib.request.Request("http://localhost:11434/api/chat", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            reply = json.loads(json.loads(r.read())["message"]["content"])
    except Exception as e:
        print(f"! local LLM chapter detection failed ({e}); using one chapter. Is `ollama serve` running?")
        return None
    return validate_llm(reply, cands) or None


def get_chapters(doc, paras, work, model, title):
    f = work / "chapters.json"
    if f.exists():
        print(f"Using saved chapters: {f}")
        return json.loads(f.read_text())
    chs = chapters_from_toc(doc, paras)
    src = "PDF outline"
    if not chs:
        print(f"No PDF outline; asking local LLM ({model}) to find chapters...")
        chs, src = chapters_from_llm(paras, model), "local LLM"
    if not chs:
        chs, src = [{"title": title, "start": 0, "skip": False}], "fallback"
    if chs[0]["start"] > 0:
        opening = sum(len(p["text"]) for p in paras[:chs[0]["start"]])
        chs.insert(0, {"title": "Opening", "start": 0, "skip": opening < 500})
    f.write_text(json.dumps(chs, indent=2, ensure_ascii=False))
    print(f"Chapters ({src}), saved to {f} - edit it and rerun to fix:")
    for c in chs:
        print(f'  {"skip" if c["skip"] else "    "}  #{c["start"]:<5} {c["title"]}')
    return chs


# ---------- 3. chunk ----------
SENT = re.compile(r'(?<=[.!?…])\s+|(?<=[.!?…]["”’)])\s+')


def chunk_paragraph(text, limit=CHUNK_CHARS):
    pieces = []
    for s in SENT.split(text):
        while len(s) > limit:  # very long sentence: cut at last comma/semicolon, else last space
            cut = max(s.rfind(c, 0, limit) for c in ",;:—")
            cut = cut + 1 if cut > limit // 3 else (s.rfind(" ", 0, limit) if s.rfind(" ", 0, limit) > 0 else limit)
            pieces.append(s[:cut].strip())
            s = s[cut:].strip()
        if s:
            pieces.append(s)
    chunks = []
    for s in pieces:
        if chunks and len(chunks[-1]) + 1 + len(s) <= limit:
            chunks[-1] += " " + s
        else:
            chunks.append(s)
    return chunks


def plan_chunks(paras, chs):
    """[(chapter_title, [(text, ends_paragraph), ...])] for non-skipped chapters."""
    out = []
    for n, c in enumerate(chs):
        end = chs[n + 1]["start"] if n + 1 < len(chs) else len(paras)
        if c["skip"]:
            continue
        items = []
        for p in paras[c["start"]:end]:
            cs = chunk_paragraph(p["text"])
            items += [(t, k == len(cs) - 1) for k, t in enumerate(cs)]
        if items:
            out.append((c["title"], items))
    return out


# ---------- 4. synthesize (one chapter at a time) ----------
_MODEL = None


def load_tts():
    global _MODEL
    if _MODEL:
        return _MODEL
    import torch
    from chatterbox.tts import ChatterboxTTS
    dev = "mps" if torch.backends.mps.is_available() else "cpu"
    try:
        _MODEL = ChatterboxTTS.from_pretrained(device=dev)
    except Exception as e:
        print(f"! MPS load failed ({e}); falling back to CPU (slower)")
        _MODEL = ChatterboxTTS.from_pretrained(device="cpu")
    return _MODEL


def free_memory():
    """Without this the MPS cache keeps growing (20+ GB on a 16 GB Mac) and generation crawls."""
    import gc, torch
    gc.collect()
    if torch.backends.mps.is_available():
        torch.mps.empty_cache()


def write_wav(path, samples_int16):
    tmp = path.with_suffix(".tmp")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes(samples_int16)
    tmp.rename(path)  # atomic, so a killed run never leaves a half chunk that looks done


def read_wav(path):
    with wave.open(str(path)) as w:
        return w.readframes(w.getnframes())


def chunk_file(work, ci, k):
    return work / f"{ci:03d}" / f"{k:05d}.wav"


def chapter_file(out_dir, ci, title):
    safe = re.sub(r'[\\/:*?"<>|]', "", title).strip() or "Chapter"
    return out_dir / f"{ci + 1:02d} - {safe}.m4a"


def synth_chapter(ci, items, work, voice, emotion, tick=None, stop=lambda: False):
    """Generate every missing chunk of chapter ci. Returns False if stopped early."""
    import numpy as np
    done = sum(chunk_file(work, ci, k).exists() for k in range(len(items)))
    if tick:
        tick(done, len(items))
    for k, (text, _) in enumerate(items):
        f = chunk_file(work, ci, k)
        if f.exists():
            continue
        if stop():
            return False
        f.parent.mkdir(parents=True, exist_ok=True)
        wav = load_tts().generate(text, audio_prompt_path=voice, exaggeration=emotion, cfg_weight=0.5)
        a = wav.squeeze().detach().cpu().numpy()
        write_wav(f, (np.clip(a, -1, 1) * 32767).astype(np.int16).tobytes())
        free_memory()
        done += 1
        if tick:
            tick(done, len(items))
    return True


# ---------- 5. export ----------
def export_chapter(ci, title, items, work, out_dir):
    """Stitch a finished chapter's chunks (with pauses) into one .m4a."""
    silence = lambda s: b"\0\0" * int(SR * s)
    audio = bytearray()
    for k, (_, ends_para) in enumerate(items):
        audio += read_wav(chunk_file(work, ci, k))
        audio += silence(PAUSE_PARA if ends_para else PAUSE_CHUNK)
    audio += silence(PAUSE_CHAPTER)
    tmp = work / f"_chapter_{ci:03d}.wav"
    write_wav(tmp, bytes(audio))
    out_dir.mkdir(parents=True, exist_ok=True)
    out = chapter_file(out_dir, ci, title)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", tmp, "-metadata", f"title={title}",
                    "-c:a", "aac", "-b:a", "64k", "-ac", "1", "-movflags", "+faststart", out], check=True)
    tmp.unlink()
    return out


def build_m4b(plan, out_dir, out_path, title):
    """Join every finished chapter .m4a (no re-encode) into one chaptered .m4b."""
    files = [chapter_file(out_dir, ci, t) for ci, (t, _) in enumerate(plan)]
    meta, pos = [";FFMETADATA1", f"title={title}"], 0
    for f, (t, _) in zip(files, plan):
        ms = int(float(subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", f])) * 1000)
        meta += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={pos}", f"END={pos + ms}", f"title={t}"]
        pos += ms
    lst, mf = out_dir / "_list.txt", out_dir / "_meta.txt"
    lst.write_text("".join(f"file '{f.resolve()}'\n" for f in files))
    mf.write_text("\n".join(meta) + "\n")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst, "-i", mf,
                    "-map", "0:a", "-map_metadata", "1", "-map_chapters", "1", "-c", "copy",
                    "-movflags", "+faststart", out_path], check=True)
    lst.unlink(); mf.unlink()
    return out_path


# ---------- the whole job ----------
def run_job(pdf, voice=None, emotion=0.6, llm="qwen3.5:9b", select=None, max_pages=None,
            out_root=None, emit=print, stop=lambda: False):
    """select: set of 1-based chapter numbers (among narrated chapters), None = all.
    emit(dict) gets events: chapters, chunk, chapter_done, finished, stopped."""
    pdf = Path(pdf)
    name = pdf.stem + (f"_p{max_pages}" if max_pages else "")
    work = HERE / "work" / name
    out_dir = (out_root or HERE / "out") / name
    work.mkdir(parents=True, exist_ok=True)
    doc, paras = extract(pdf, max_pages)
    if sum(len(p["text"]) for p in paras) < 200:
        raise ValueError("Almost no text found - this PDF is probably scanned images (OCR is not supported).")
    title = doc.metadata.get("title") or pdf.stem
    chs = get_chapters(doc, paras, work, llm, title)
    plan = plan_chunks(paras, chs)
    emit({"type": "chapters", "name": name,
          "chapters": [{"n": i + 1, "title": t, "chunks": len(it), "chars": sum(len(x) for x, _ in it),
                        "done": chapter_file(out_dir, i, t).exists(),
                        "have": sum(chunk_file(work, i, k).exists() for k in range(len(it)))}
                       for i, (t, it) in enumerate(plan)]})
    for ci, (t, items) in enumerate(plan):
        if select is not None and ci + 1 not in select:
            continue
        if chapter_file(out_dir, ci, t).exists():
            continue
        ok = synth_chapter(ci, items, work, voice, emotion,
                           tick=lambda d, n, ci=ci: emit({"type": "chunk", "n": ci + 1, "done": d, "total": n}),
                           stop=stop)
        if not ok:
            return emit({"type": "stopped"})
        path = export_chapter(ci, t, items, work, out_dir)
        emit({"type": "chapter_done", "n": ci + 1, "file": path.name})
    if all(chapter_file(out_dir, ci, t).exists() for ci, (t, _) in enumerate(plan)):
        emit({"type": "finished", "m4b": build_m4b(plan, out_dir, out_dir / f"{name}.m4b", title).name})
    else:
        emit({"type": "finished", "m4b": None})


# ---------- self-check ----------
def selftest():
    text = ("Hello there. " * 40) + "A" * 50 + ", " + "word " * 120 + "end! “Quoted.” Next one?"
    cs = chunk_paragraph(text)
    assert all(len(c) <= CHUNK_CHARS for c in cs), max(map(len, cs))
    assert re.sub(r"\s", "", "".join(cs)) == re.sub(r"\s", "", text), "text lost"
    assert all(c.endswith((".", "!", "?", "”")) for c in chunk_paragraph("One. Two! Three? “Four.”"))
    cands = [{"id": 3, "text": "Chapter 1"}, {"id": 9, "text": "Chapter 2"}]
    got = validate_llm({"chapters": [{"id": 9, "title": "Two"}, {"id": 3, "title": "One"}, {"id": 99}, {"id": "x"}]}, cands)
    assert [g["start"] for g in got] == [9], got
    assert validate_llm("garbage", cands) == []
    assert FRONT.search("Copyright") and FRONT.search("Table of Contents") and not FRONT.search("Chapter Two")
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pdf", nargs="?")
    ap.add_argument("--voice", help="10-20 s .wav of the narrator voice to clone (default: built-in voice)")
    ap.add_argument("--emotion", type=float, default=0.6, help="0.5 = neutral, 0.7+ = more expressive")
    ap.add_argument("--llm", default="qwen3.5:9b", help="Ollama model used to find chapters")
    ap.add_argument("--chapters", help="only these chapters, e.g. 2 or 2,3 (numbered among narrated chapters)")
    ap.add_argument("--max-pages", type=int, help="only use the first N pages (for a quick sample)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.pdf:
        ap.error("give a PDF")
    sel = {int(x) for x in a.chapters.split(",")} if a.chapters else None

    def show(e):
        if e["type"] == "chunk":
            print(f'\r  chapter {e["n"]}: {e["done"]}/{e["total"]}   ', end="", flush=True)
        elif e["type"] == "chapter_done":
            print(f'\n  saved {e["file"]}')
        elif e["type"] == "finished":
            print("Full audiobook:", e["m4b"] or "(not all chapters done yet)")
    run_job(a.pdf, a.voice, a.emotion, a.llm, sel, a.max_pages, emit=show)


if __name__ == "__main__":
    main()

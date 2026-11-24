# PDF to audiobook (local, free)
Setup (done): `uv venv -p 3.11 .venv && uv pip install -p .venv/bin/python -r requirements.txt`
Run: `.venv/bin/python make_audiobook.py book.pdf [--voice voices/me.wav] [--emotion 0.6] [--llm qwen3.5:9b]`
- Output: `out/<book>.m4b` with chapters (Apple Books). Rerun after a stop and it resumes.
- Chapters: PDF outline, else local Ollama LLM. Edit `work/<book>/chapters.json` (set "skip": true) and rerun to fix.
- `--max-pages 5` for a quick sample, `--selftest` for the chunker check.

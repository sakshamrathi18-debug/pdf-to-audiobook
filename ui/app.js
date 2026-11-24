const $ = (id) => document.getElementById(id);
let S = { books: {}, current: {}, voices: [], queued: 0 };
let selected = decodeURIComponent(location.hash.slice(1)) || null;
const checked = new Set();
let playing = null;       // audio file name currently loaded
let failed = null;        // last request error
let lastVoices = "";

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const mins = (chars) => Math.max(1, Math.round(chars / 15 / 60));   // about 15 spoken characters a second
const audioUrl = (book, file) => `/audio/${encodeURIComponent(book)}/${encodeURIComponent(file)}`;

async function api(path, opts) {
  try {
    const r = await fetch(path, opts);
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
    failed = null;
    return await r.json();
  } catch (e) {
    failed = e.message === "Failed to fetch" ? "Cannot reach the server. Is server.py still running?" : e.message;
    return null;
  }
}

async function poll() {
  const s = await api("/api/state");
  if (s) S = s;
  render();
  setTimeout(poll, S.current.book ? 1500 : 4000);
}

function pick(name) {
  selected = name;
  checked.clear();
  location.hash = encodeURIComponent(name);
  render();
}

function render() {
  const names = Object.keys(S.books);
  if (!selected || !S.books[selected]) selected = names[0] || null;

  $("books").innerHTML = names.map((n) => {
    const b = S.books[n], done = b.chapters.filter((c) => c.done).length;
    return `<li><button data-book="${esc(n)}" aria-current="${n === selected}"><span class="name">${esc(n)}</span>` +
           `<span class="count">${b.preparing ? "reading" : `${done}/${b.chapters.length}`}</span></button></li>`;
  }).join("") || `<li class="hint">No books yet.</li>`;

  const cur = S.current;
  $("status").innerHTML = failed ? esc(failed)
    : cur.book && cur.chapter ? `Converting <b>${esc(cur.book)}</b>, chapter ${cur.chapter}${S.queued ? `, ${S.queued} more queued` : ""}`
    : cur.book ? `Reading <b>${esc(cur.book)}</b>`
    : "Idle";

  const sv = $("voice");
  const key = S.voices.join("|");
  if (key !== lastVoices) {
    const v = sv.value;
    sv.innerHTML = `<option value="">Default voice</option>` + S.voices.map((x) => `<option>${esc(x)}</option>`).join("");
    sv.value = S.voices.includes(v) ? v : "";
    lastVoices = key;
  }

  $("main").innerHTML = selected ? bookView(selected) : emptyView();
}

function emptyView() {
  return `<div class="note"><strong>Add a PDF to begin.</strong><p>It reads the chapters first. Then you pick which to convert, and each chapter is playable the moment it finishes.</p></div>`;
}

function bookView(name) {
  const b = S.books[name], cur = S.current;
  if (b.error) return `<h2>${esc(name)}</h2><div class="note error">${esc(b.error)}</div>`;
  if (b.preparing) return `<h2>${esc(name)}</h2><p class="hint">Reading chapters...</p>` + `<div class="skeleton"></div>`.repeat(5);

  const cs = b.chapters, busy = cur.book === name;
  const ready = cs.filter((c) => c.done).length;
  const left = cs.length - ready;
  const rows = cs.map((c) => {
    const active = busy && cur.chapter === c.n;
    const waiting = busy && !c.done && !active && cur.select.includes(c.n);
    let state;
    if (c.done) state = `<span class="state ready">Ready</span>`;
    else if (active || c.progress > 0) {
      const pct = Math.round(c.progress * 100);
      state = `<span class="state"><span class="meter"><i style="--p:${c.progress}"></i></span>${active ? "" : "paused "}${pct}%</span>`;
    } else state = `<span class="state">${waiting ? "Waiting" : "Not converted"}</span>`;
    return `<tr class="${playing === c.file ? "playing" : ""}">
      <td class="chk"><input type="checkbox" data-n="${c.n}" aria-label="Select ${esc(c.title)}" ${c.done ? "disabled" : ""} ${checked.has(c.n) ? "checked" : ""}></td>
      <td class="num">${c.n}</td><td class="title" title="${esc(c.title)}">${esc(c.title)}</td>
      <td class="num len">${mins(c.chars)} min</td><td>${state}</td>
      <td>${c.done ? `<button class="btn play" data-play="${esc(c.file)}" data-title="${esc(c.title)}" data-n="${c.n}">Play</button>` : ""}</td></tr>`;
  }).join("");

  const n = [...checked].filter((x) => cs[x - 1] && !cs[x - 1].done).length;
  return `<div class="head"><h2>${esc(name)}</h2><p>${ready} of ${cs.length} chapters ready</p></div>
    <div class="tools">
      <button class="btn primary" data-go="selected" ${n && !failed ? "" : "disabled"}>Convert selected${n ? ` (${n})` : ""}</button>
      <button class="btn" data-go="rest" ${left ? "" : "disabled"}>Convert all remaining</button>
      <button class="btn" data-stop ${busy || S.queued ? "" : "disabled"}>Stop</button>
      ${b.m4b ? `<a class="btn" href="${audioUrl(name, b.m4b)}" download>Download full audiobook</a>` : ""}
    </div>
    <table><thead><tr><th class="chk"></th><th class="num">#</th><th>Chapter</th><th class="num len">Length</th><th>Status</th><th></th></tr></thead><tbody>${rows}</tbody></table>
    <p class="hint">Finished chapters are saved in out/${esc(name)}. Stopping keeps what is done, and converting again carries on from there.</p>`;
}

function play(file, title, n) {
  playing = file;
  const a = $("audio");
  a.src = audioUrl(selected, file);
  a.play().catch(() => {});
  $("now").innerHTML = `${esc(title)}<small>${esc(selected)}, chapter ${n}</small>`;
  render();
}

async function start(which) {
  const cs = S.books[selected].chapters;
  const list = which === "rest" ? cs.filter((c) => !c.done).map((c) => c.n) : [...checked].filter((x) => !cs[x - 1].done);
  if (!list.length) return;
  await api("/api/start", { method: "POST", body: JSON.stringify({
    book: selected, chapters: list, emotion: $("emotion").value, voice: $("voice").value || null }) });
  checked.clear();
  const s = await api("/api/state");
  if (s) S = s;
  render();
}

async function upload(file, path) {
  if (!file) return;
  const r = await api(`${path}?name=${encodeURIComponent(file.name)}`, { method: "POST", body: file });
  return r;
}

document.addEventListener("click", async (e) => {
  const t = e.target.closest("[data-book],[data-play],[data-go],[data-stop]");
  if (!t) return;
  if (t.dataset.book) pick(t.dataset.book);
  else if (t.dataset.play) play(t.dataset.play, t.dataset.title, t.dataset.n);
  else if (t.dataset.go) start(t.dataset.go);
  else if ("stop" in t.dataset) { await api("/api/stop", { method: "POST" }); poll(); }
});
document.addEventListener("change", (e) => {
  if (e.target.dataset.n) {
    e.target.checked ? checked.add(+e.target.dataset.n) : checked.delete(+e.target.dataset.n);
    render();
  }
});
$("emotion").addEventListener("input", (e) => ($("emoOut").value = (+e.target.value).toFixed(2)));

const drop = $("drop");
async function addPdf(f) {
  if (!f || !/\.pdf$/i.test(f.name)) { failed = "That is not a PDF."; return render(); }
  const r = await upload(f, "/api/upload");
  if (r) { S.books[r.name] = { chapters: [], m4b: null, error: null, preparing: true }; pick(r.name); }
  else render();
}
$("file").addEventListener("change", (e) => addPdf(e.target.files[0]));
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => addPdf(e.dataTransfer.files[0]));
$("voicefile").addEventListener("change", async (e) => {
  const r = await upload(e.target.files[0], "/api/voice");
  if (r) { const s = await api("/api/state"); if (s) { S = s; render(); $("voice").value = r.name; } }
});

// keep listening: when a chapter ends, start the next finished one
$("audio").addEventListener("ended", () => {
  const cs = (S.books[selected] || {}).chapters || [];
  const i = cs.findIndex((c) => c.file === playing);
  const next = cs.slice(i + 1).find((c) => c.done);
  if (next) play(next.file, next.title, next.n);
});

poll();

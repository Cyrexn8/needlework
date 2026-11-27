"use strict";

/* Needlework — ядро: канвас-просмотрщик схемы с прогрессом и подсветкой цвета. */

const RULER = 22;            // ширина полосы линеек, px
const MIN_SCALE = 3, MAX_SCALE = 60;
const SYMBOL_SCALE = 9;      // крупнее этого — рисуем символы
const CHECK_SCALE = 14;      // крупнее этого — рисуем галочки на вышитом

const canvas = document.getElementById("canvas");
const ctx = canvas.getContext("2d");
const wrap = document.getElementById("canvasWrap");
const paletteList = document.getElementById("paletteList");
const paletteAll = document.getElementById("paletteAll");
const palettePanel = document.getElementById("palettePanel");

let P = null;                          // pattern
let paletteById = new Map();
let done = new Map();                  // "x,y" -> 1
let selected = null;                   // id выбранного цвета (или null)
let view = { scale: 16, ox: 0, oy: 0 };// ox/oy — смещение мира на экране
let saveTimer = null;

const dpr = () => window.devicePixelRatio || 1;
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const cssW = () => canvas.width / dpr();
const cssH = () => canvas.height / dpr();

/* Смешать цвет со светлым серым — «вышито/стёрто». */
function washedColor(rgb) {
  const n = parseInt(rgb.slice(1), 16);
  const mix = (v) => Math.round(v * 0.25 + 216 * 0.75);
  return `rgb(${mix((n >> 16) & 255)},${mix((n >> 8) & 255)},${mix(n & 255)})`;
}

/* ---------- хранилище: IndexedDB с фолбэком в память ---------- */

const idbMem = new Map();   // если IndexedDB недоступен (приватный режим Safari)
let idbDb = null;

function openIdb() {
  if (idbDb) return Promise.resolve(idbDb);
  return new Promise((res, rej) => {
    const rq = indexedDB.open("needlework", 1);
    rq.onupgradeneeded = () => rq.result.createObjectStore("kv");
    rq.onsuccess = () => { idbDb = rq.result; res(idbDb); };
    rq.onerror = () => rej(rq.error);
  });
}

async function idbGet(key) {
  try {
    const db = await openIdb();
    return await new Promise((res, rej) => {
      const t = db.transaction("kv", "readonly").objectStore("kv").get(key);
      t.onsuccess = () => res(t.result);
      t.onerror = () => rej(t.error);
    });
  } catch (e) { return idbMem.get(key); }
}

async function idbSet(key, val) {
  try {
    const db = await openIdb();
    await new Promise((res, rej) => {
      const t = db.transaction("kv", "readwrite").objectStore("kv").put(val, key);
      t.onsuccess = () => res();
      t.onerror = () => rej(t.error);
    });
  } catch (e) { idbMem.set(key, val); }
}

async function idbDel(key) {
  try {
    const db = await openIdb();
    await new Promise((res, rej) => {
      const t = db.transaction("kv", "readwrite").objectStore("kv").delete(key);
      t.onsuccess = () => res();
      t.onerror = () => rej(t.error);
    });
  } catch (e) { idbMem.delete(key); }
}

async function idbKeys() {
  try {
    const db = await openIdb();
    return await new Promise((res, rej) => {
      const t = db.transaction("kv", "readonly").objectStore("kv").getAllKeys();
      t.onsuccess = () => res(t.result);
      t.onerror = () => rej(t.error);
    });
  } catch (e) { return [...idbMem.keys()]; }
}

/* ---------- бэкенд: локальный сервер или статика (GitHub Pages) ---------- */

let MODE = "static";             // "server" | "static" — определяется автоматически
let builtins = [];               // встроенные схемы из data/index.json (статический режим)

async function detectMode() {
  try {
    const r = await fetch("api/projects");
    if (r.ok && Array.isArray(await r.json())) { MODE = "server"; return; }
  } catch (e) { /* это не наш сервер */ }
  MODE = "static";
}

async function listProjects() {
  if (MODE === "server") return (await (await fetch("api/projects")).json());
  const out = [...builtins];
  for (const k of await idbKeys()) {
    if (!k.startsWith("pattern:")) continue;
    const p = await idbGet(k);
    out.push({ id: k.slice(8), name: p.name || k.slice(8), width: p.width, height: p.height });
  }
  return out;
}

async function loadPattern(id) {
  if (MODE === "server") {
    const r = await fetch(`api/pattern?project=${encodeURIComponent(id)}`);
    if (!r.ok) throw new Error("Не удалось загрузить схему");
    return r.json();
  }
  const b = builtins.find(x => x.id === id);
  if (b) {
    const r = await fetch(`data/${encodeURIComponent(id)}.json`);
    if (r.ok) return r.json();
  }
  const p = await idbGet(`pattern:${id}`);
  if (p) return p;
  throw new Error("Схема не найдена");
}

async function loadProgress(id) {
  if (MODE === "server") {
    const r = await fetch(`api/progress?project=${encodeURIComponent(id)}`);
    return r.ok ? r.json() : { done: {} };
  }
  return (await idbGet(`progress:${id}`)) || { done: {} };
}

async function saveProgressData(id, doneObj) {
  if (MODE === "server") {
    await fetch(`api/progress?project=${encodeURIComponent(id)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ done: doneObj }),
      keepalive: true,
    });
    return;
  }
  await idbSet(`progress:${id}`, { done: doneObj });
}

/* ---------- загрузка ---------- */

async function load() {
  const project = currentProject();
  const [pattern, prog] = await Promise.all([loadPattern(project), loadProgress(project)]);
  P = pattern;
  paletteById = new Map(P.palette.map(p => {
    p._washed = washedColor(p.rgb);   // «стёртый» вариант цвета для вышитых клеток
    return [p.id, p];
  }));
  done = new Map(Object.entries(prog.done || {}).map(([k, v]) => [k, v]));

  document.getElementById("patternName").textContent = P.name;
  buildPalette();
  updateCounts();
  resizeCanvas();   // измеряем канвас до вписывания вида
  fitView();
  draw();
}

/* ---------- проекты и импорт ---------- */

function currentProject() {
  return new URLSearchParams(location.search).get("project") || "demo";
}

async function initProjects() {
  const sel = document.getElementById("projectSelect");
  try {
    const list = await listProjects();
    sel.innerHTML = "";
    for (const p of list) {
      const o = document.createElement("option");
      o.value = p.id;
      o.textContent = `${p.name} (${p.width}×${p.height})`;
      sel.appendChild(o);
    }
  } catch (e) { /* сервер мог ещё не собрать проекты */ }
  sel.value = currentProject();
  if (![...sel.options].some(o => o.value === currentProject()) && sel.options.length) {
    sel.value = sel.options[0].value;
  }
  sel.addEventListener("change", () => {
    location.search = "?project=" + encodeURIComponent(sel.value);
  });
}

const btnImport = document.getElementById("btnImport");
const fileImport = document.getElementById("fileImport");
btnImport.addEventListener("click", () => fileImport.click());
fileImport.addEventListener("change", async () => {
  const f = fileImport.files[0];
  if (!f) return;
  const label = btnImport.textContent;
  btnImport.disabled = true;
  btnImport.textContent = "⏳";
  try {
    if (MODE === "server") {
      const fd = new FormData();
      fd.append("file", f);
      const res = await fetch("api/import/pdf", { method: "POST", body: fd });
      const data = await res.json();
      if (!res.ok) {
        alert("Импорт не удался: " + (data.detail || res.statusText));
        return;
      }
      location.search = "?project=" + encodeURIComponent(data.project);
    } else {
      const r = await importDataFile(f);
      if (r.kind === "backup") {
        await initProjects();
        alert("Бэкап восстановлен: схем " + r.count +
              ", файлов прогресса " + r.progressCount);
      } else {
        location.search = "?project=" + encodeURIComponent(r.id);
      }
    }
  } catch (e) {
    alert("Ошибка импорта: " + e.message);
  } finally {
    btnImport.disabled = false;
    btnImport.textContent = label;
    fileImport.value = "";
  }
});

/* Импорт файла в статическом режиме: pattern.json или файл бэкапа. */
function slugName(s) {
  const t = (s || "").replace(/[^\wа-яё-]+/gi, "_").replace(/^_+|_+$/g, "");
  return (t || "schema").slice(0, 60);
}

async function importDataFile(file) {
  let j;
  try { j = JSON.parse(await file.text()); }
  catch (e) { throw new Error("файл не является JSON"); }
  if (j && Array.isArray(j.patterns)) {                     // файл бэкапа
    let progressCount = 0;
    for (const it of j.patterns) await idbSet(`pattern:${it.id}`, it.data);
    if (j.progress && typeof j.progress === "object") {
      for (const [k, v] of Object.entries(j.progress)) {
        await idbSet(`progress:${k}`, { done: v || {} });
        progressCount++;
      }
    }
    return { kind: "backup", count: j.patterns.length, progressCount };
  }
  if (j && Array.isArray(j.grid) && Array.isArray(j.palette)
      && Number.isFinite(j.width) && Number.isFinite(j.height)) {   // схема
    const used = new Set(builtins.map(b => b.id));
    for (const k of await idbKeys()) if (k.startsWith("pattern:")) used.add(k.slice(8));
    const base = slugName(j.name || file.name.replace(/\.json$/i, ""));
    let id = base;
    for (let n = 2; used.has(id); n++) id = `${base}_${n}`;
    await idbSet(`pattern:${id}`, j);
    return { kind: "pattern", id };
  }
  throw new Error("файл не похож на схему или бэкап");
}

/* Экспорт всего содержимого хранилища в файл-бэкап (только статический режим). */
async function exportAll() {
  const progress = {}, patterns = [];
  for (const k of await idbKeys()) {
    if (k.startsWith("progress:")) {
      const v = await idbGet(k);
      progress[k.slice(9)] = (v && v.done) || {};
    } else if (k.startsWith("pattern:")) {
      patterns.push({ id: k.slice(8), data: await idbGet(k) });
    }
  }
  return { app: "needlework", version: 1, date: new Date().toISOString(), patterns, progress };
}

function downloadJSON(obj, filename) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([JSON.stringify(obj)], { type: "application/json" }));
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

const btnExport = document.getElementById("btnExport");
btnExport.addEventListener("click", async () => {
  try {
    downloadJSON(await exportAll(),
      `needlework-backup-${new Date().toISOString().slice(0, 10)}.json`);
  } catch (e) { alert("Не удалось экспортировать: " + e.message); }
});

const btnDelete = document.getElementById("btnDelete");
btnDelete.addEventListener("click", async () => {
  const id = currentProject();
  if (builtins.some(b => b.id === id)) { alert("Встроенную схему удалить нельзя"); return; }
  if (!confirm(`Удалить схему «${id}» и её прогресс с этого устройства?`)) return;
  await idbDel(`pattern:${id}`);
  await idbDel(`progress:${id}`);
  location.search = "?project=demo";
});

/* ---------- палитра ---------- */

function buildPalette() {
  paletteList.innerHTML = "";
  for (const p of P.palette) {
    const btn = document.createElement("button");
    btn.className = "palette-item";
    btn.dataset.id = p.id;
    btn.title = `${p.dmc} ${p.name}`;
    btn.innerHTML =
      `<span class="swatch" style="background:${p.rgb}">${p.symbol}</span>` +
      `<span class="pal-dmc">${p.dmc}</span>` +
      `<span class="pal-name">${p.name}</span>` +
      `<span class="pal-count" id="cnt-${p.id}"></span>`;
    btn.addEventListener("click", () => {
      selected = selected === p.id ? null : p.id;
      syncPaletteActive();
      draw();
    });
    paletteList.appendChild(btn);
  }
}

paletteAll.addEventListener("click", () => {
  selected = null;
  syncPaletteActive();
  draw();
});

function syncPaletteActive() {
  paletteList.querySelectorAll(".palette-item").forEach(el => {
    el.classList.toggle("active", Number(el.dataset.id) === selected);
  });
  paletteAll.classList.toggle("active", selected === null);
}

/* ---------- прогресс ---------- */

function updateCounts() {
  const perColor = new Map();
  for (const key of done.keys()) {
    const [x, y] = key.split(",").map(Number);
    const c = P.grid[y] && P.grid[y][x];
    if (c >= 0) perColor.set(c, (perColor.get(c) || 0) + 1);
  }
  for (const p of P.palette) {
    const el = document.getElementById(`cnt-${p.id}`);
    if (el) el.textContent = `${perColor.get(p.id) || 0}/${p.count}`;
  }
  const total = P.palette.reduce((s, p) => s + p.count, 0);
  const donePct = total ? done.size / total * 100 : 0;
  document.getElementById("progressBar").style.width = donePct.toFixed(1) + "%";
  const textEl = document.getElementById("progressText");
  textEl.textContent = `осталось ${Math.max(0, Math.round(100 - donePct))}%`;
  textEl.title = `Вышито ${done.size} из ${total} (${donePct.toFixed(1)}%)`;
}

function scheduleSave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveProgress, 600);
}

async function saveProgress() {
  try {
    await saveProgressData(currentProject(), Object.fromEntries(done));
  } catch (e) { /* при следующем изменении попробуем снова */ }
}

window.addEventListener("pagehide", () => { clearTimeout(saveTimer); saveProgress(); });

/* ---------- отмена изменений (undo/redo) ---------- */

const btnUndo = document.getElementById("btnUndo");
const btnRedo = document.getElementById("btnRedo");
let undoStack = [];          // партии изменений (одна партия = одно действие пользователя)
let redoStack = [];
let currentBatch = null;     // формируемая партия — пока идёт мазок кисти
const MAX_HISTORY = 200;

function beginBatch() { currentBatch = []; }

function recordChange(key, before, after) {
  if (currentBatch) currentBatch.push({ key, before, after });
}

function commitBatch() {
  if (!currentBatch) return;
  if (currentBatch.length) {
    undoStack.push(currentBatch);
    if (undoStack.length > MAX_HISTORY) undoStack.shift();
    redoStack = [];        // новое действие обрывает ветку возврата
  }
  currentBatch = null;
  syncHistoryButtons();
}

function applyBatch(batch, dir) {          // dir: "undo" | "redo"
  for (const { key, before, after } of batch) {
    if ((dir === "undo" ? before : after) === 1) done.set(key, 1);
    else done.delete(key);
  }
  updateCounts();
  scheduleSave();
  draw();
}

function undo() {
  if (currentBatch || !undoStack.length) return;
  const batch = undoStack.pop();
  applyBatch(batch, "undo");
  redoStack.push(batch);
  syncHistoryButtons();
}

function redo() {
  if (currentBatch || !redoStack.length) return;
  const batch = redoStack.pop();
  applyBatch(batch, "redo");
  undoStack.push(batch);
  syncHistoryButtons();
}

function syncHistoryButtons() {
  btnUndo.disabled = undoStack.length === 0;
  btnRedo.disabled = redoStack.length === 0;
}

btnUndo.addEventListener("click", undo);
btnRedo.addEventListener("click", redo);

window.addEventListener("keydown", (e) => {
  if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
  const k = e.key.toLowerCase();
  if (k === "z") { e.preventDefault(); e.shiftKey ? redo() : undo(); }
  else if (k === "y") { e.preventDefault(); redo(); }
});

/* ---------- координаты и вид ---------- */

function fitView() {
  const s = clamp(Math.min((cssW() - RULER - 10) / P.width, (cssH() - RULER - 10) / P.height),
                  MIN_SCALE, MAX_SCALE);
  view.scale = s;
  view.ox = (cssW() - RULER - P.width * s) / 2;
  view.oy = (cssH() - RULER - P.height * s) / 2;
}

function zoomAt(factor, sx, sy) {
  const ns = clamp(view.scale * factor, MIN_SCALE, MAX_SCALE);
  const wx = (sx - RULER - view.ox) / view.scale;
  const wy = (sy - RULER - view.oy) / view.scale;
  view.scale = ns;
  view.ox = sx - RULER - wx * ns;
  view.oy = sy - RULER - wy * ns;
  draw();
}

function modifyCellAt(cx, cy, action) {   // "toggle" | "mark" | "unmark"
  const wx = Math.floor((cx - RULER - view.ox) / view.scale);
  const wy = Math.floor((cy - RULER - view.oy) / view.scale);
  if (wx < 0 || wy < 0 || wx >= P.width || wy >= P.height) return;
  const c = P.grid[wy][wx];
  if (c < 0) return;
  const key = `${wx},${wy}`;
  const had = done.has(key);
  if (action === "mark" && had) return;
  if (action === "unmark" && !had) return;
  if (action !== "toggle" && selected !== null && c !== selected) return;
  beginBatch();
  if (had) done.delete(key); else done.set(key, 1);
  recordChange(key, had ? 1 : 0, had ? 0 : 1);
  commitBatch();
  updateCounts();
  scheduleSave();
  draw();
}

/* ---------- отрисовка ---------- */

function resizeCanvas() {
  const r = wrap.getBoundingClientRect();
  if (r.width === 0 || r.height === 0) return;
  canvas.width = Math.round(r.width * dpr());
  canvas.height = Math.round(r.height * dpr());
  draw();
}
new ResizeObserver(resizeCanvas).observe(wrap);

function draw() {
  ctx.setTransform(dpr(), 0, 0, dpr(), 0, 0);
  ctx.fillStyle = "#fbf9f4";
  ctx.fillRect(0, 0, cssW(), cssH());
  if (!P || !paletteById.size) return;

  const { scale, ox, oy } = view;
  const x0 = Math.max(0, Math.floor((0 - RULER - ox) / scale));
  const x1 = Math.min(P.width - 1, Math.floor((cssW() - RULER - ox) / scale));
  const y0 = Math.max(0, Math.floor((0 - RULER - oy) / scale));
  const y1 = Math.min(P.height - 1, Math.floor((cssH() - RULER - oy) / scale));

  // клетки
  for (let y = y0; y <= y1; y++) {
    for (let x = x0; x <= x1; x++) {
      const c = P.grid[y][x];
      if (c < 0) continue;
      const p = paletteById.get(c);
      const sx = RULER + x * scale + ox;
      const sy = RULER + y * scale + oy;
      const rx = Math.round(sx), rw = Math.round(sx + scale) - rx;
      const ry = Math.round(sy), rh = Math.round(sy + scale) - ry;

      const isDone = done.has(`${x},${y}`);
      if (selected !== null && c !== selected) {
        // невыбранные цвета — бледные, но различимые
        ctx.globalAlpha = 0.30;
        ctx.fillStyle = isDone ? p._washed : p.rgb;
        ctx.fillRect(rx, ry, rw, rh);
        ctx.globalAlpha = 1;
        continue;
      }
      ctx.fillStyle = isDone ? p._washed : p.rgb;
      ctx.fillRect(rx, ry, rw, rh);
      if (isDone && scale >= CHECK_SCALE) {
        ctx.fillStyle = "rgba(110,105,95,0.5)";
        ctx.font = `bold ${Math.round(scale * 0.6)}px "Segoe UI Symbol", sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText("✓", rx + rw / 2, ry + rh / 2 + 1);
      }
      if (scale >= SYMBOL_SCALE) {
        ctx.fillStyle = isDone ? "rgba(115,110,100,0.4)" : "rgba(30,25,20,0.75)";
        ctx.font = `${Math.round(scale * 0.62)}px "Segoe UI Symbol","Noto Sans Symbols 2",sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(p.symbol, rx + rw / 2, ry + rh / 2 + 1);
      }
    }
  }

  // сетка
  ctx.strokeStyle = "rgba(0,0,0,0.08)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (let x = x0; x <= x1 + 1; x++) {
    const px = Math.round(RULER + x * scale + ox) + 0.5;
    ctx.moveTo(px, RULER + oy);
    ctx.lineTo(px, RULER + (y1 + 1) * scale + oy);
  }
  for (let y = y0; y <= y1 + 1; y++) {
    const py = Math.round(RULER + y * scale + oy) + 0.5;
    ctx.moveTo(RULER + ox, py);
    ctx.lineTo(RULER + (x1 + 1) * scale + ox, py);
  }
  ctx.stroke();

  // линии через каждые 10 клеток
  ctx.strokeStyle = "rgba(0,0,0,0.30)";
  ctx.beginPath();
  for (let x = Math.ceil((x0 + 1) / 10) * 10; x <= x1 + 1; x += 10) {
    const px = Math.round(RULER + x * scale + ox) + 0.5;
    ctx.moveTo(px, RULER + oy);
    ctx.lineTo(px, RULER + (y1 + 1) * scale + oy);
  }
  for (let y = Math.ceil((y0 + 1) / 10) * 10; y <= y1 + 1; y += 10) {
    const py = Math.round(RULER + y * scale + oy) + 0.5;
    ctx.moveTo(RULER + ox, py);
    ctx.lineTo(RULER + (x1 + 1) * scale + ox, py);
  }
  ctx.stroke();

  // рамка схемы
  ctx.strokeStyle = "rgba(0,0,0,0.35)";
  ctx.strokeRect(RULER + ox, RULER + oy, P.width * scale, P.height * scale);

  // линейки
  ctx.fillStyle = "#f1ece3";
  ctx.fillRect(0, 0, cssW(), RULER);
  ctx.fillRect(0, 0, RULER, cssH());
  ctx.fillStyle = "rgba(58,51,43,0.85)";
  ctx.font = '11px "Segoe UI", sans-serif';
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  for (let x = Math.ceil(Math.max(1, x0 + 1) / 10) * 10; x <= x1 + 1; x += 10) {
    ctx.fillText(String(x), RULER + x * scale + ox, RULER / 2);
  }
  for (let y = Math.ceil(Math.max(1, y0 + 1) / 10) * 10; y <= y1 + 1; y += 10) {
    ctx.fillText(String(y), RULER / 2, RULER + y * scale + oy);
  }
}

/* ---------- ввод: мышь, тач, колесо ---------- */

const MODES = ["pan", "paint", "erase"];
const MODE_BTNS = {
  pan: document.getElementById("btnModePan"),
  paint: document.getElementById("btnModePaint"),
  erase: document.getElementById("btnModeErase"),
};
let mode = MODES.includes(localStorage.getItem("nw-mode"))
  ? localStorage.getItem("nw-mode") : "pan";

function syncMode() {
  for (const m of MODES) MODE_BTNS[m].classList.toggle("active", mode === m);
  canvas.classList.toggle("paint-mode", mode !== "pan");   // курсор-крестик
}
for (const m of MODES) {
  MODE_BTNS[m].addEventListener("click", () => {
    mode = m;
    localStorage.setItem("nw-mode", m);
    syncMode();
  });
}
syncMode();

const pointers = new Map();   // pointerId -> {x, y, pans}
let pinchStart = null;
let tapCandidate = null;
let stroke = null;            // активный мазок кисти
let multiTouch = false;       // был жест 2+ пальцами — до отпускания всех не рисуем

function canvasPos(e) {
  const rect = canvas.getBoundingClientRect();
  return { x: e.clientX - rect.left, y: e.clientY - rect.top };
}

function setCellDone(cx, cy) {
  const wx = Math.floor((cx - RULER - view.ox) / view.scale);
  const wy = Math.floor((cy - RULER - view.oy) / view.scale);
  if (wx < 0 || wy < 0 || wx >= P.width || wy >= P.height) return;
  const c = P.grid[wy][wx];
  if (c < 0) return;                                    // пустая клетка
  if (selected !== null && c !== selected) return;      // выбран цвет — красим только его
  const key = `${wx},${wy}`;
  if (!done.has(key)) {
    done.set(key, 1);
    recordChange(key, 0, 1);
    updateCounts();
  }
}

function paintSegment(fromX, fromY, toX, toY, applyCell) {
  const dist = Math.hypot(toX - fromX, toY - fromY);
  const steps = Math.max(1, Math.ceil(dist / (view.scale * 0.4)));
  for (let i = 1; i <= steps; i++) {
    const t = i / steps;
    applyCell(fromX + (toX - fromX) * t, fromY + (toY - fromY) * t);
  }
}

function setCellUndone(cx, cy) {
  const wx = Math.floor((cx - RULER - view.ox) / view.scale);
  const wy = Math.floor((cy - RULER - view.oy) / view.scale);
  if (wx < 0 || wy < 0 || wx >= P.width || wy >= P.height) return;
  const c = P.grid[wy][wx];
  if (c < 0) return;
  if (selected !== null && c !== selected) return;      // выбран цвет — стираем только его
  const key = `${wx},${wy}`;
  if (done.has(key)) {
    done.delete(key);
    recordChange(key, 1, 0);
    updateCounts();
  }
}

canvas.addEventListener("pointerdown", (e) => {
  const pans = mode === "pan" || e.button === 2;   // правая кнопка всегда панорама
  pointers.set(e.pointerId, { x: e.clientX, y: e.clientY, pans });
  try { canvas.setPointerCapture(e.pointerId); } catch {}  // не критично, если захват не удался
  if (pointers.size === 1) {
    tapCandidate = { sx: e.clientX, sy: e.clientY, t: performance.now(), moved: false };
    stroke = null;
    if (pans) canvas.classList.add("dragging");
  }
  if (pointers.size === 2) {
    tapCandidate = null;
    stroke = null;
    multiTouch = true;
    canvas.classList.add("dragging");
    const [a, b] = [...pointers.values()];
    pinchStart = {
      dist: Math.hypot(a.x - b.x, a.y - b.y),
      scale: view.scale,
      midx: (a.x + b.x) / 2,
      midy: (a.y + b.y) / 2,
      ox: view.ox,
      oy: view.oy,
    };
  }
});

canvas.addEventListener("pointermove", (e) => {
  const prev = pointers.get(e.pointerId);
  if (!prev) return;
  const dx = e.clientX - prev.x;
  const dy = e.clientY - prev.y;
  pointers.set(e.pointerId, { x: e.clientX, y: e.clientY, pans: prev.pans });

  if (pointers.size === 1) {
    if (tapCandidate &&
        Math.hypot(e.clientX - tapCandidate.sx, e.clientY - tapCandidate.sy) > 8) {
      tapCandidate.moved = true;
    }
    if (prev.pans || multiTouch) {   // после pinch-жеста палец панирует, а не рисует
      view.ox += dx;
      view.oy += dy;
      draw();
    } else if (mode === "paint" || mode === "erase") {
      const applyCell = mode === "paint" ? setCellDone : setCellUndone;
      const pt = canvasPos(e);
      if (!stroke) {
        const startX = tapCandidate ? tapCandidate.sx : e.clientX;
        const startY = tapCandidate ? tapCandidate.sy : e.clientY;
        const rect = canvas.getBoundingClientRect();
        if (Math.hypot(e.clientX - startX, e.clientY - startY) < 4) return; // порог мазка
        stroke = { lastX: startX - rect.left, lastY: startY - rect.top };
        beginBatch();
      }
      paintSegment(stroke.lastX, stroke.lastY, pt.x, pt.y, applyCell);
      stroke.lastX = pt.x;
      stroke.lastY = pt.y;
      if (tapCandidate) tapCandidate.moved = true;
      scheduleSave();
      draw();
    }
  } else if (pointers.size >= 2 && pinchStart) {
    const [a, b] = [...pointers.values()];
    const dist = Math.hypot(a.x - b.x, a.y - b.y) || 1;
    const midx = (a.x + b.x) / 2, midy = (a.y + b.y) / 2;
    const k = dist / pinchStart.dist;
    view.scale = clamp(pinchStart.scale * k, MIN_SCALE, MAX_SCALE);
    const wx = (pinchStart.midx - RULER - pinchStart.ox) / pinchStart.scale;
    const wy = (pinchStart.midy - RULER - pinchStart.oy) / pinchStart.scale;
    view.ox = midx - RULER - wx * view.scale;
    view.oy = midy - RULER - wy * view.scale;
    draw();
  }
});

function endPointer(e) {
  if (!pointers.has(e.pointerId)) return;
  pointers.delete(e.pointerId);
  canvas.classList.remove("dragging");
  if (pointers.size < 2) pinchStart = null;
  if (pointers.size === 0) {
    stroke = null;
    multiTouch = false;
    commitBatch();   // завершаем партию мазка (пустая — просто отбросится)
  }
  if (tapCandidate && !tapCandidate.moved && performance.now() - tapCandidate.t < 500
      && pointers.size === 0) {
    const pt = canvasPos(e);
    modifyCellAt(pt.x, pt.y,
      mode === "paint" ? "mark" : mode === "erase" ? "unmark" : "toggle");
  }
  if (pointers.size === 0) tapCandidate = null;
}
canvas.addEventListener("pointerup", endPointer);
canvas.addEventListener("pointercancel", endPointer);

canvas.addEventListener("wheel", (e) => {
  e.preventDefault();
  const rect = canvas.getBoundingClientRect();
  const factor = Math.exp(-e.deltaY * 0.0015);
  zoomAt(factor, e.clientX - rect.left, e.clientY - rect.top);
}, { passive: false });

canvas.addEventListener("contextmenu", (e) => e.preventDefault());

/* ---------- кнопки ---------- */

document.getElementById("btnZoomIn").addEventListener("click", () =>
  zoomAt(1.25, cssW() / 2, cssH() / 2));
document.getElementById("btnZoomOut").addEventListener("click", () =>
  zoomAt(0.8, cssW() / 2, cssH() / 2));
document.getElementById("btnFit").addEventListener("click", () => { fitView(); draw(); });
document.getElementById("btnPalette").addEventListener("click", () =>
  palettePanel.classList.toggle("open"));

/* ---------- запуск ---------- */

(async () => {
  await detectMode();
  document.body.classList.toggle("static", MODE === "static");
  if (MODE === "static") {
    builtins = await (await fetch("data/index.json")).json();
    fileImport.accept = ".json,application/json";
    btnImport.title = "Импортировать схему (pattern.json) или файл бэкапа";
  }
  await initProjects();
  await load();
})().catch(err => {
  document.getElementById("patternName").textContent = "Ошибка загрузки";
  console.error(err);
});

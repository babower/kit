// kit-charts: dependency-free SVG charts for server-rendered dashboards.
// Column (stacked), line and horizontal-bar charts, tooltips (mouse and keyboard), a table
// view for every chart, light/dark theme toggle, narrower drawings on phones. Themed only
// through CSS variables (charts.css), so it follows the page's theme.
//
// Works under a strict Content-Security-Policy: inline it in a <script nonce="..."> and it
// never sets a `style` attribute (it uses element.style, which CSP allows).
//
// The page provides: an element with id "tip" (the tooltip), optionally a button with id
// "theme", and a `window.renderAll()` that draws everything (called again when the theme or
// the screen width changes). Optional: window.KIT_CHARTS = { themeKey: "my-app-theme" }.
"use strict";
const $ = (id) => document.getElementById(id);
const NS = "http://www.w3.org/2000/svg";
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const money = (v, d = 2) => v == null ? "–" : "$" + v.toLocaleString(undefined,
  { minimumFractionDigits: d, maximumFractionDigits: d });
const num = (v) => v == null ? "–" : Math.round(v).toLocaleString();
const pct = (v) => v == null ? "–" : Math.round(v * 100) + "%";
const compact = (v) => v == null ? "–" : v >= 1e6 ? (v / 1e6).toFixed(1) + "M"
  : v >= 1e3 ? (v / 1e3).toFixed(v >= 1e4 ? 0 : 1) + "k" : String(Math.round(v));
const shortDay = (s) => { const d = new Date(s.slice(0, 10) + "T00:00:00Z");
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" }); };
const when = (iso) => iso ? shortDay(iso) + " " + iso.slice(11, 16) + " UTC" : "–";
const plural = (n, word) => `${num(n)} ${word}${n === 1 ? "" : "s"}`;
function h(tag, attrs = {}, text) { const n = document.createElement(tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]); if (text != null) n.textContent = text; return n; }
function s(tag, attrs = {}, parent) { const n = document.createElementNS(NS, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]); if (parent) parent.appendChild(n); return n; }
const SERIES = ["--series-1", "--series-2", "--series-3", "--series-4", "--series-5", "--series-6"];
const seriesColor = (i) => i == null || i >= SERIES.length ? css("--neutral") : css(SERIES[i]);

// ---- Theme toggle -----------------------------------------------------------------------
const THEME_KEY = (window.KIT_CHARTS && window.KIT_CHARTS.themeKey) || "kit-theme";
try { const t = localStorage.getItem(THEME_KEY); if (t) document.documentElement.dataset.theme = t; } catch (e) {}
if ($("theme")) $("theme").addEventListener("click", () => {
  const dark = document.documentElement.dataset.theme
    ? document.documentElement.dataset.theme === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.dataset.theme = dark ? "light" : "dark";
  try { localStorage.setItem(THEME_KEY, document.documentElement.dataset.theme); } catch (e) {}
  if (window.renderAll) window.renderAll();
});
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => window.renderAll && window.renderAll());

// ---- Tooltip ------------------------------------------------------------------------------
const tip = $("tip");
function showTip(ev, title, rows) {
  tip.replaceChildren(h("div", { class: "t" }, title));
  for (const r of rows) {
    const row = h("div", { class: "r" });
    if (r.color) { const k = h("span", { class: "k" }); k.style.background = r.color; row.appendChild(k); }
    row.appendChild(h("b", {}, r.value));
    row.appendChild(h("span", { class: "muted" }, r.label));
    tip.appendChild(row);
  }
  tip.style.display = "block";
  const x = Math.min(ev.clientX + 14, innerWidth - tip.offsetWidth - 8);
  const y = Math.min(ev.clientY + 14, innerHeight - tip.offsetHeight - 8);
  tip.style.left = Math.max(8, x) + "px"; tip.style.top = Math.max(8, y) + "px";
}
const hideTip = () => { tip.style.display = "none"; };
function hoverable(node, show) {
  node.setAttribute("tabindex", 0);
  node.addEventListener("pointermove", show); node.addEventListener("pointerleave", hideTip);
  node.addEventListener("focus", () => { const b = node.getBoundingClientRect();
    show({ clientX: b.left + b.width / 2, clientY: b.top }); });
  node.addEventListener("blur", hideTip);
}

function legend(items) {  // items: { label, color, line? } (line: a thin line, not a box)
  const l = h("div", { class: "legend" });
  for (const it of items) { const sp = h("span"); const i = h("i", it.line ? { class: "line" } : {});
    i.style.background = it.color; sp.append(i, document.createTextNode(it.label)); l.appendChild(sp); }
  return l;
}
function table(headers, rows) {
  const t = h("table"); const tr = h("tr");
  headers.forEach((x) => tr.appendChild(h("th", { class: x.num ? "num" : "" }, x.label)));
  t.appendChild(tr);
  for (const r of rows) { const row = h("tr");
    r.forEach((v, i) => { const td = h("td", { class: headers[i].num ? "num" : "" });
      if (v instanceof Node) td.appendChild(v); else td.textContent = v; row.appendChild(td); });
    t.appendChild(row); }
  const wrap = h("div", { class: "scroll" }); wrap.appendChild(t); return wrap;
}
function tableView(headers, rows, label = "Show table") {
  const det = h("details", { class: "table" }); det.appendChild(h("summary", {}, label));
  det.appendChild(table(headers, rows)); return det;
}
function statusChip(cls, text) { return h("span", { class: "status " + cls }, text); }
function empty(el, text) { el.appendChild(h("p", { class: "muted empty" }, text)); }
// Narrow containers (phones) get a narrower drawing so text stays readable when scaled.
const narrow = (el) => el.clientWidth > 0 && el.clientWidth < 520;
const drawWidth = (el, width) => width || (narrow(el) ? 400 : 640);
let resizeTimer = null, lastNarrow = innerWidth < 600;
addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => {
  if ((innerWidth < 600) !== lastNarrow || innerWidth > 900) { lastNarrow = innerWidth < 600;
    if (window.renderAll) window.renderAll(); } }, 200); });
function niceMax(v) { if (v <= 0) return 1; const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * p >= v) return m * p; return 10 * p; }

// ---- Stacked / single columns over days ------------------------------------------------------
function columnChart(el, { labels, series, fmt, height = 220, tickFmt = shortDay, extraBelow, width }) {
  const W = drawWidth(el, width), H = height, L = 48, R = 8, T = 8, B = 22 + (extraBelow ? 18 : 0);
  const svg = s("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img" });
  const totals = labels.map((_, i) => series.reduce((a, se) => a + (se.values[i] || 0), 0));
  const max = niceMax(Math.max(...totals, 0));
  const y = (v) => T + (H - T - B) * (1 - v / max);
  for (let k = 0; k <= 4; k++) { const v = max * k / 4;
    s("line", { class: "gridline", x1: L, x2: W - R, y1: y(v), y2: y(v) }, svg);
    s("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end" }, svg).textContent = fmt(v, true); }
  const n = labels.length, slot = (W - L - R) / n, bw = Math.max(2, Math.min(28, slot - 2));
  const every = Math.ceil(n / 8);
  labels.forEach((lab, i) => {
    const x = L + i * slot + (slot - bw) / 2; const g = s("g", { class: "col" }, svg);
    let acc = 0; const segs = series.filter((se) => (se.values[i] || 0) > 0);
    segs.forEach((se, j) => { const v = se.values[i]; const top = y(acc + v), bot = y(acc);
      const gap = j > 0 ? 2 : 0; const hgt = Math.max(0, bot - top - gap);
      if (j === segs.length - 1) s("path", { class: "mark", fill: se.color,
        d: roundedTop(x, top, bw, hgt, Math.min(4, bw / 2, hgt)) }, g);
      else s("rect", { class: "mark", x, y: top, width: bw, height: hgt, fill: se.color }, g);
      acc += v; });
    const hit = s("rect", { class: "hit", x: L + i * slot, y: T, width: slot, height: H - T - B }, g);
    hoverable(hit, (ev) => showTip(ev, tickFmt(lab), [...series].reverse().map((se) =>
      ({ color: se.color, value: fmt(se.values[i] || 0), label: se.label }))
      .concat(series.length > 1 ? [{ value: fmt(totals[i]), label: "Total" }] : [])));
    if (i % every === 0) s("text", { x: L + i * slot + slot / 2, y: H - B + 15, "text-anchor": "middle" }, svg)
      .textContent = tickFmt(lab);
    if (extraBelow) extraBelow(svg, i, L + i * slot + slot / 2, H - 8);
  });
  s("line", { class: "axis", x1: L, x2: W - R, y1: y(0), y2: y(0) }, svg);
  el.appendChild(svg);
}
function roundedTop(x, y, w, h, r) {
  if (h <= 0) return ""; r = Math.max(0, Math.min(r, h));
  return `M${x},${y + h} V${y + r} Q${x},${y} ${x + r},${y} H${x + w - r} Q${x + w},${y} ${x + w},${y + r} V${y + h} Z`;
}

// ---- Line chart (single series) -----------------------------------------------------------
function lineChart(el, { labels, values, fmt, color, height = 200, title = (l) => shortDay(l) }) {
  const W = drawWidth(el), H = height, L = 48, R = 12, T = 10, B = 22;
  const svg = s("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img" });
  const vals = values.filter((v) => v != null); const max = niceMax(Math.max(...vals, 0));
  const y = (v) => T + (H - T - B) * (1 - v / max);
  const x = (i) => L + (labels.length === 1 ? (W - L - R) / 2 : i * (W - L - R) / (labels.length - 1));
  for (let k = 0; k <= 4; k++) { const v = max * k / 4;
    s("line", { class: "gridline", x1: L, x2: W - R, y1: y(v), y2: y(v) }, svg);
    s("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end" }, svg).textContent = fmt(v, true); }
  let d = "", pen = false;
  values.forEach((v, i) => { if (v == null) { pen = false; return; }
    d += (pen ? "L" : "M") + x(i) + "," + y(v); pen = true; });
  s("path", { d, fill: "none", stroke: color, "stroke-width": 2, "stroke-linejoin": "round" }, svg);
  values.forEach((v, i) => { if (v != null) s("circle", { cx: x(i), cy: y(v), r: 4, fill: color,
    stroke: css("--surface-1"), "stroke-width": 2 }, svg); });
  const cross = s("line", { class: "axis", y1: T, y2: H - B, opacity: 0 }, svg);
  const every = Math.ceil(labels.length / 8);
  labels.forEach((lab, i) => { if (i % every === 0) s("text", { x: x(i), y: H - 6, "text-anchor": "middle" }, svg)
    .textContent = shortDay(lab); });
  const hit = s("rect", { class: "hit", x: L, y: T, width: W - L - R, height: H - T - B }, svg);
  hit.addEventListener("pointermove", (ev) => {
    const b = svg.getBoundingClientRect(); const px = (ev.clientX - b.left) * W / b.width;
    let best = 0; labels.forEach((_, i) => { if (Math.abs(x(i) - px) < Math.abs(x(best) - px)) best = i; });
    cross.setAttribute("x1", x(best)); cross.setAttribute("x2", x(best)); cross.setAttribute("opacity", 1);
    showTip(ev, title(labels[best]), [{ color, value: fmt(values[best]), label: "" }]);
  });
  hit.addEventListener("pointerleave", () => { cross.setAttribute("opacity", 0); hideTip(); });
  s("line", { class: "axis", x1: L, x2: W - R, y1: y(0), y2: y(0) }, svg);
  el.appendChild(svg);
}

// ---- Horizontal bars ---------------------------------------------------------------------------
function hbars(el, rows, { color, fmt, label, note: noteFn, max: fixedMax }) {
  let note = noteFn;
  // On phones the notes move to the tooltip and the table, leaving room for the bars.
  const small = narrow(el), W = small ? 400 : 640, rowH = 30, L = small ? 112 : 150,
    R = small ? 56 : 160, H = rows.length * rowH + 4;
  if (small) note = null;
  const svg = s("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img" });
  const max = fixedMax || niceMax(Math.max(...rows.map((r) => r.value), 0));
  rows.forEach((r, i) => {
    const y = i * rowH + 6, w = Math.max(r.value > 0 ? 2 : 0, (W - L - R) * r.value / max);
    s("text", { x: L - 8, y: y + 13, "text-anchor": "end", class: "lbl" }, svg).textContent = label(r);
    s("rect", { x: L, y: y + 2, width: W - L - R, height: 14, fill: css("--grid"), rx: 3 }, svg);
    s("path", { class: "mark", d: roundedRight(L, y + 2, w, 14, 4), fill: r.color || color }, svg);
    s("text", { x: W - R + 8, y: y + 13, class: "lbl-strong" }, svg).textContent = fmt(r.value);
    if (note) s("text", { x: W - R + 62, y: y + 13 }, svg).textContent = note(r);
    const hit = s("rect", { class: "hit", x: 0, y, width: W, height: rowH - 2 }, svg);
    hoverable(hit, (ev) => showTip(ev, label(r),
      [{ color: r.color || color, value: fmt(r.value), label: noteFn ? noteFn(r) : "" }]));
  });
  el.appendChild(svg);
}
function roundedRight(x, y, w, h, r) { if (w <= 0) return ""; r = Math.min(r, w, h / 2);
  return `M${x},${y} H${x + w - r} Q${x + w},${y} ${x + w},${y + r} V${y + h - r} Q${x + w},${y + h} ${x + w - r},${y + h} H${x} Z`; }

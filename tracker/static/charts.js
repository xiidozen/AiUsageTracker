// Small SVG chart helpers: line, stacked column, scatter and heatmap.
// Colors are CSS custom properties so light/dark themes switch without redraws.

const NS = "http://www.w3.org/2000/svg";

export function svgEl(tag, attrs = {}, parent = null) {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null) continue;
    if (k === "style") Object.assign(node.style, v);
    else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v);
  }
  if (parent) parent.appendChild(node);
  return node;
}

export function htmlEl(tag, attrs = {}, parent = null) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null) continue;
    if (k === "style") Object.assign(node.style, v);
    else if (k === "text") node.textContent = v;
    else if (k === "class") node.className = v;
    else node.setAttribute(k, v);
  }
  if (parent) parent.appendChild(node);
  return node;
}

// -- tooltip -------------------------------------------------------------

let tooltipNode = null;

function tooltip() {
  if (!tooltipNode) {
    tooltipNode = htmlEl("div", { class: "tooltip", role: "status", hidden: "" }, document.body);
  }
  return tooltipNode;
}

// rows: [{color, value, label, key: "line" | "rect"}]
export function showTooltip(x, y, title, rows) {
  const tip = tooltip();
  tip.replaceChildren();
  if (title) htmlEl("div", { class: "t-title", text: title }, tip);
  for (const row of rows) {
    const line = htmlEl("div", { class: "t-row" }, tip);
    if (row.color) {
      htmlEl("span", { class: row.key === "rect" ? "key-rect" : "key-line", style: { background: row.color } }, line);
    }
    htmlEl("strong", { text: row.value }, line);
    htmlEl("span", { text: row.label }, line);
  }
  tip.hidden = false;
  const pad = 14;
  const { width, height } = tip.getBoundingClientRect();
  let left = x + pad;
  let top = y + pad;
  if (left + width > window.innerWidth - 8) left = x - width - pad;
  if (top + height > window.innerHeight - 8) top = y - height - pad;
  tip.style.left = `${Math.max(8, left)}px`;
  tip.style.top = `${Math.max(8, top)}px`;
}

export function hideTooltip() {
  if (tooltipNode) tooltipNode.hidden = true;
}

// -- shared helpers --------------------------------------------------------

function prepare(container, height) {
  container.replaceChildren();
  const width = Math.max(container.clientWidth || 600, 280);
  const svg = svgEl("svg", { width, height, viewBox: `0 0 ${width} ${height}`, role: "img" }, container);
  return { svg, width };
}

export function niceMax(value, fallback = 1) {
  if (!(value > 0)) return fallback;
  const exp = Math.pow(10, Math.floor(Math.log10(value)));
  for (const m of [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) {
    if (m * exp >= value) return m * exp;
  }
  return 10 * exp;
}

function ticks(max, count) {
  // Prefer a tick count that gives whole-number steps (30 -> 0, 6, 12, ...).
  if (!count) count = [4, 5, 3, 6].find((n) => Number.isInteger(+(max / n).toFixed(6))) || 4;
  const step = max / count;
  return Array.from({ length: count + 1 }, (_, i) => +(i * step).toFixed(6));
}

function compact(v) {
  if (Math.abs(v) >= 1000) return `${+(v / 1000).toFixed(1)}K`;
  return `${+v.toFixed(1)}`;
}

function timeTicks(t0, t1, maxTicks) {
  const span = t1 - t0;
  const hour = 3600;
  const day = 86400;
  const steps = [hour, 3 * hour, 6 * hour, 12 * hour, day, 2 * day, 7 * day, 14 * day, 30 * day, 61 * day, 91 * day];
  const step = steps.find((s) => span / s <= maxTicks) || 182 * day;
  const out = [];
  const start = new Date(t0 * 1000);
  if (step >= day) start.setHours(0, 0, 0, 0);
  else start.setMinutes(0, 0, 0);
  if (step >= 7 * day && step < 30 * day) {
    // Align weekly ticks to Monday.
    start.setDate(start.getDate() - ((start.getDay() + 6) % 7));
  }
  if (step >= 30 * day) start.setDate(1);
  let t = start.getTime() / 1000;
  while (t <= t1) {
    if (t >= t0) out.push(t);
    if (step >= 30 * day) {
      const d = new Date(t * 1000);
      d.setMonth(d.getMonth() + Math.round(step / (30 * day)));
      t = d.getTime() / 1000;
    } else if (step >= day) {
      const d = new Date(t * 1000);
      d.setDate(d.getDate() + Math.round(step / day));
      t = d.getTime() / 1000;
    } else {
      t += step;
    }
  }
  return { ticks: out, step };
}

function formatTick(t, step) {
  const d = new Date(t * 1000);
  if (step < 86400) {
    if (d.getHours() === 0) return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
    return d.toLocaleTimeString(undefined, { hour: "numeric" });
  }
  if (step >= 30 * 86400) return d.toLocaleDateString(undefined, { month: "short", year: "2-digit" });
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function roundedTopRect(x, y, w, h, r) {
  r = Math.min(r, w / 2, h);
  if (h <= 0) return "";
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

function emptyState(svg, width, height, message) {
  svgEl("text", { x: width / 2, y: height / 2, "text-anchor": "middle", class: "empty", text: message }, svg);
}

function observe(container, draw) {
  draw();
  if (container._ro) container._ro.disconnect();
  let lastWidth = container.clientWidth;
  container._ro = new ResizeObserver(() => {
    if (Math.abs(container.clientWidth - lastWidth) > 2) {
      lastWidth = container.clientWidth;
      draw();
    }
  });
  container._ro.observe(container);
}

// -- line chart --------------------------------------------------------------

// opts: {series: [{key, label, color, points: [[t, v|null], ...]}], domain: [t0, t1],
//        yMax, height, bands: [[a, b]], yFormat, endLabels, ariaLabel, empty}
export function lineChart(container, opts) {
  observe(container, () => drawLine(container, opts));
}

function drawLine(container, opts) {
  const height = opts.height || 260;
  const { svg, width } = prepare(container, height);
  svg.setAttribute("aria-label", opts.ariaLabel || "Line chart");
  const m = { top: 10, right: opts.endLabels ? 44 : 16, bottom: 26, left: 40 };
  const pw = width - m.left - m.right;
  const ph = height - m.top - m.bottom;
  const [t0, t1] = opts.domain;
  const yMax = opts.yMax || 100;
  const fmt = opts.yFormat || ((v) => `${Math.round(v)}%`);
  const x = (t) => m.left + ((t - t0) / Math.max(t1 - t0, 1)) * pw;
  const y = (v) => m.top + ph - (Math.min(v, yMax) / yMax) * ph;

  for (const [a, b] of opts.bands || []) {
    const xa = Math.max(x(a), m.left);
    const xb = Math.min(x(b), m.left + pw);
    if (xb > xa) svgEl("rect", { x: xa, y: m.top, width: xb - xa, height: ph, style: { fill: "var(--blind)" } }, svg);
  }
  for (const v of ticks(yMax)) {
    svgEl("line", { x1: m.left, x2: m.left + pw, y1: y(v), y2: y(v), class: v === 0 ? "axis-line" : "grid-line" }, svg);
    svgEl("text", { x: m.left - 8, y: y(v) + 4, "text-anchor": "end", text: fmt(v) }, svg);
  }
  const { ticks: xt, step } = timeTicks(t0, t1, Math.max(2, Math.floor(pw / 90)));
  for (const t of xt) {
    svgEl("text", { x: x(t), y: height - 8, "text-anchor": "middle", text: formatTick(t, step) }, svg);
  }

  const visible = opts.series.filter((s) => !s.hidden);
  const hasData = visible.some((s) => s.points.some((p) => p[1] !== null));
  if (!hasData) {
    emptyState(svg, width, height, opts.empty || "No data yet");
    return;
  }

  for (const s of visible) {
    let d = "";
    let pen = false;
    for (const [t, v] of s.points) {
      if (v === null || v === undefined) {
        pen = false;
        continue;
      }
      d += `${pen ? "L" : "M"}${x(t).toFixed(1)},${y(v).toFixed(1)}`;
      pen = true;
    }
    svgEl("path", {
      d,
      style: { fill: "none", stroke: s.color, strokeWidth: "2px", strokeLinejoin: "round", strokeLinecap: "round" },
    }, svg);
  }

  if (opts.endLabels) {
    const ends = visible
      .map((s) => {
        const last = [...s.points].reverse().find((p) => p[1] !== null);
        return last ? { s, t: last[0], v: last[1] } : null;
      })
      .filter(Boolean);
    const ys = ends.map((e) => y(e.v)).sort((a, b) => a - b);
    const collide = ys.some((v, i) => i > 0 && v - ys[i - 1] < 14);
    for (const e of ends) {
      svgEl("circle", { cx: x(e.t), cy: y(e.v), r: 4, style: { fill: e.s.color, stroke: "var(--surface)", strokeWidth: "2px" } }, svg);
      if (!collide) {
        svgEl("text", { x: x(e.t) + 8, y: y(e.v) + 4, class: "label-strong", text: fmt(e.v) }, svg);
      }
    }
  }

  // Hover layer: crosshair snaps to the nearest sample; one tooltip lists every series.
  const hover = svgEl("g", { style: { pointerEvents: "none" } }, svg);
  const overlay = svgEl("rect", { x: m.left, y: m.top, width: pw, height: ph, class: "hit" }, svg);
  const nearest = (points, t) => {
    let lo = 0;
    let hi = points.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (points[mid][0] < t) lo = mid + 1;
      else hi = mid;
    }
    let best = null;
    for (const i of [lo - 1, lo, lo + 1]) {
      const p = points[i];
      if (p && p[1] !== null && (!best || Math.abs(p[0] - t) < Math.abs(best[0] - t))) best = p;
    }
    return best && Math.abs(best[0] - t) <= (t1 - t0) / 40 ? best : null;
  };
  let cursorT = null;
  const update = (t, clientX, clientY) => {
    cursorT = t;
    hover.replaceChildren();
    const found = visible.map((s) => ({ s, p: nearest(s.points, t) }));
    const anchor = found.find((f) => f.p);
    if (!anchor) {
      hideTooltip();
      return;
    }
    const at = anchor.p[0];
    svgEl("line", { x1: x(at), x2: x(at), y1: m.top, y2: m.top + ph, class: "crosshair" }, hover);
    for (const f of found) {
      if (f.p) svgEl("circle", { cx: x(f.p[0]), cy: y(f.p[1]), r: 4, style: { fill: f.s.color, stroke: "var(--surface)", strokeWidth: "2px" } }, hover);
    }
    const title = new Date(at * 1000).toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
    const rows = found.map((f) => ({ color: f.s.color, value: f.p ? fmt(f.p[1]) : "–", label: f.s.label }));
    if (clientX === undefined) {
      const box = svg.getBoundingClientRect();
      clientX = box.left + x(at);
      clientY = box.top + m.top;
    }
    showTooltip(clientX, clientY, title, rows);
  };
  overlay.addEventListener("pointermove", (ev) => {
    const box = svg.getBoundingClientRect();
    const px = ((ev.clientX - box.left) / box.width) * width;
    update(t0 + ((px - m.left) / pw) * (t1 - t0), ev.clientX, ev.clientY);
  });
  const clear = () => {
    hover.replaceChildren();
    hideTooltip();
  };
  overlay.addEventListener("pointerleave", clear);
  container.tabIndex = 0;
  container.onkeydown = (ev) => {
    if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
    ev.preventDefault();
    const stepT = (t1 - t0) / 60;
    const base = cursorT === null ? t1 : cursorT;
    update(Math.min(t1, Math.max(t0, base + (ev.key === "ArrowLeft" ? -stepT : stepT))));
  };
  container.onblur = clear;
}

// -- stacked column chart -------------------------------------------------------

// opts: {categories: [{label, title, values: {key: n}}], stack: [{key, label, color}],
//        yMax, height, refLine: {value, label}, capLabels, yFormat, valueFormat, ariaLabel, empty}
export function columnChart(container, opts) {
  observe(container, () => drawColumns(container, opts));
}

function drawColumns(container, opts) {
  const height = opts.height || 240;
  const { svg, width } = prepare(container, height);
  svg.setAttribute("aria-label", opts.ariaLabel || "Column chart");
  const m = { top: 18, right: 12, bottom: 26, left: 40 };
  const pw = width - m.left - m.right;
  const ph = height - m.top - m.bottom;
  const cats = opts.categories;
  const fmtAxis = opts.yFormat || compact;
  const fmtValue = opts.valueFormat || ((v) => `${+v.toFixed(1)}`);
  if (!cats.length) {
    emptyState(svg, width, height, opts.empty || "No data yet");
    return;
  }
  const totals = cats.map((c) => opts.stack.reduce((sum, s) => sum + (c.values[s.key] || 0), 0));
  const yMax = opts.yMax || niceMax(Math.max(...totals, opts.refLine ? opts.refLine.value : 0));
  const y = (v) => m.top + ph - (Math.min(v, yMax) / yMax) * ph;
  for (const v of ticks(yMax)) {
    svgEl("line", { x1: m.left, x2: m.left + pw, y1: y(v), y2: y(v), class: v === 0 ? "axis-line" : "grid-line" }, svg);
    svgEl("text", { x: m.left - 8, y: y(v) + 4, "text-anchor": "end", text: fmtAxis(v) }, svg);
  }
  const band = pw / cats.length;
  const barW = Math.max(4, Math.min(24, band * 0.62));
  const labelEvery = Math.max(1, Math.ceil(56 / band));
  const gap = 2;

  cats.forEach((c, i) => {
    const cx = m.left + band * (i + 0.5);
    const g = svgEl("g", { class: "mark" }, svg);
    const segs = opts.stack.map((s) => ({ s, v: c.values[s.key] || 0 })).filter((p) => p.v > 0);
    let base = 0;
    segs.forEach((seg, j) => {
      const top = base + seg.v;
      const yTop = y(top);
      const yBottom = y(base);
      const isTop = j === segs.length - 1;
      let h = yBottom - yTop - (isTop ? 0 : gap);
      if (h > 0) {
        svgEl("path", { d: isTop ? roundedTopRect(cx - barW / 2, yTop, barW, h, 4) : `M${cx - barW / 2},${yTop + gap}h${barW}v${h}h${-barW}Z`, style: { fill: seg.s.color } }, g);
      }
      base = top;
    });
    if (opts.capLabels && cats.length <= 16 && totals[i] > 0) {
      svgEl("text", { x: cx, y: y(totals[i]) - 5, "text-anchor": "middle", class: "label-strong", text: fmtValue(totals[i]) }, svg);
    }
    if (i % labelEvery === 0) {
      svgEl("text", { x: cx, y: height - 8, "text-anchor": "middle", text: c.label }, svg);
    }
    const hit = svgEl("rect", { x: cx - band / 2, y: m.top, width: band, height: ph, class: "hit", tabindex: 0 }, svg);
    const show = (ev) => {
      g.classList.add("hovered");
      const rows = opts.stack.map((s) => ({ color: s.color, key: "rect", value: fmtValue(c.values[s.key] || 0), label: s.label }));
      let x = ev && ev.clientX;
      let yy = ev && ev.clientY;
      if (x === undefined) {
        const box = hit.getBoundingClientRect();
        x = box.left + box.width / 2;
        yy = box.top;
      }
      showTooltip(x, yy, c.title || c.label, rows.concat(c.extraRows || []));
    };
    const hide = () => {
      g.classList.remove("hovered");
      hideTooltip();
    };
    hit.addEventListener("pointermove", show);
    hit.addEventListener("pointerleave", hide);
    hit.addEventListener("focus", () => show());
    hit.addEventListener("blur", hide);
  });

  if (opts.refLine) {
    const ry = y(opts.refLine.value);
    svgEl("line", { x1: m.left, x2: m.left + pw, y1: ry, y2: ry, class: "ref-line" }, svg);
    svgEl("text", { x: m.left + pw, y: ry - 5, "text-anchor": "end", class: "label-strong", text: opts.refLine.label }, svg);
  }
}

// -- scatter -------------------------------------------------------------------

// opts: {points: [{x, y, title, rows}], xMax, yMax, color, fit: {slope, label},
//        xLabel, yLabel, height, ariaLabel, empty}
export function scatterChart(container, opts) {
  observe(container, () => drawScatter(container, opts));
}

function drawScatter(container, opts) {
  const height = opts.height || 260;
  const { svg, width } = prepare(container, height);
  svg.setAttribute("aria-label", opts.ariaLabel || "Scatter chart");
  const m = { top: 14, right: 16, bottom: 42, left: 48 };
  const pw = width - m.left - m.right;
  const ph = height - m.top - m.bottom;
  const xMax = opts.xMax || 100;
  const pts = opts.points;
  const yMax = opts.yMax || niceMax(Math.max(...pts.map((p) => p.y), opts.fit ? opts.fit.slope * xMax : 0, 1));
  const x = (v) => m.left + (Math.min(v, xMax) / xMax) * pw;
  const y = (v) => m.top + ph - (Math.min(v, yMax) / yMax) * ph;
  for (const v of ticks(yMax)) {
    svgEl("line", { x1: m.left, x2: m.left + pw, y1: y(v), y2: y(v), class: v === 0 ? "axis-line" : "grid-line" }, svg);
    svgEl("text", { x: m.left - 8, y: y(v) + 4, "text-anchor": "end", text: `${compact(v)}%` }, svg);
  }
  for (const v of ticks(xMax, 5)) {
    svgEl("text", { x: x(v), y: m.top + ph + 16, "text-anchor": "middle", text: `${compact(v)}%` }, svg);
  }
  if (opts.xLabel) svgEl("text", { x: m.left + pw / 2, y: height - 4, "text-anchor": "middle", class: "label-strong", text: opts.xLabel }, svg);
  if (opts.yLabel) {
    svgEl("text", { x: 12, y: m.top + ph / 2, "text-anchor": "middle", class: "label-strong", transform: `rotate(-90 12 ${m.top + ph / 2})`, text: opts.yLabel }, svg);
  }
  if (!pts.length) {
    emptyState(svg, width, height, opts.empty || "No data yet");
    return;
  }
  if (opts.fit && opts.fit.slope > 0) {
    const xEnd = Math.min(xMax, yMax / opts.fit.slope);
    svgEl("line", { x1: x(0), y1: y(0), x2: x(xEnd), y2: y(opts.fit.slope * xEnd), style: { stroke: "var(--text-2)", strokeWidth: "1.5px" } }, svg);
    if (opts.fit.label) {
      svgEl("text", { x: x(xEnd) - 4, y: y(opts.fit.slope * xEnd) - 8, "text-anchor": "end", class: "label-strong", text: opts.fit.label }, svg);
    }
  }
  const dots = pts.map((p) =>
    svgEl("circle", { cx: x(p.x), cy: y(p.y), r: 4, style: { fill: opts.color, stroke: "var(--surface)", strokeWidth: "2px" } }, svg)
  );
  const overlay = svgEl("rect", { x: m.left - 12, y: m.top - 12, width: pw + 24, height: ph + 24, class: "hit" }, svg);
  let active = null;
  const highlight = (i, clientX, clientY) => {
    if (active !== null) dots[active].setAttribute("r", 4);
    active = i;
    if (i === null) {
      hideTooltip();
      return;
    }
    dots[i].setAttribute("r", 6);
    showTooltip(clientX, clientY, pts[i].title, pts[i].rows);
  };
  overlay.addEventListener("pointermove", (ev) => {
    const box = svg.getBoundingClientRect();
    const scale = width / box.width;
    const px = (ev.clientX - box.left) * scale;
    const py = (ev.clientY - box.top) * scale;
    let best = null;
    let bestD = 24 * 24;
    pts.forEach((p, i) => {
      const d = (x(p.x) - px) ** 2 + (y(p.y) - py) ** 2;
      if (d < bestD) {
        bestD = d;
        best = i;
      }
    });
    highlight(best, ev.clientX, ev.clientY);
  });
  overlay.addEventListener("pointerleave", () => highlight(null));
}

// -- heatmap ---------------------------------------------------------------------

// opts: {rows: [label], cols: [label], values: [[n]], title(r, c, v), steps: [css colors], ariaLabel}
export function heatmap(container, opts) {
  observe(container, () => drawHeatmap(container, opts));
}

function drawHeatmap(container, opts) {
  const rowsN = opts.rows.length;
  const colsN = opts.cols.length;
  const m = { top: 6, right: 6, bottom: 22, left: 40 };
  const width0 = Math.max(container.clientWidth || 600, 280);
  const cell = Math.max(8, Math.min(28, (width0 - m.left - m.right) / colsN));
  const height = m.top + m.bottom + cell * rowsN;
  const { svg } = prepare(container, height);
  svg.setAttribute("aria-label", opts.ariaLabel || "Heatmap");
  const max = Math.max(1, ...opts.values.flat());
  const steps = opts.steps;
  const color = (v) => (v <= 0 ? steps[0] : steps[Math.min(steps.length - 1, 1 + Math.floor(((v - 1) / max) * (steps.length - 1)))]);
  const labelEvery = cell < 18 ? 6 : 3;
  opts.rows.forEach((r, i) => {
    svgEl("text", { x: m.left - 8, y: m.top + cell * i + cell / 2 + 4, "text-anchor": "end", text: r }, svg);
  });
  opts.cols.forEach((c, j) => {
    if (j % labelEvery === 0) svgEl("text", { x: m.left + cell * j + cell / 2, y: height - 6, "text-anchor": "middle", text: c }, svg);
  });
  opts.values.forEach((row, i) => {
    row.forEach((v, j) => {
      const rect = svgEl("rect", {
        x: m.left + cell * j + 1,
        y: m.top + cell * i + 1,
        width: cell - 2,
        height: cell - 2,
        rx: 2,
        tabindex: 0,
        class: "mark",
        style: { fill: color(v) },
      }, svg);
      const show = (ev) => {
        rect.classList.add("hovered");
        let cx = ev && ev.clientX;
        let cy = ev && ev.clientY;
        if (cx === undefined) {
          const b = rect.getBoundingClientRect();
          cx = b.right;
          cy = b.top;
        }
        showTooltip(cx, cy, opts.title(i, j), [{ value: String(v), label: opts.valueLabel || "" }]);
      };
      const hide = () => {
        rect.classList.remove("hovered");
        hideTooltip();
      };
      rect.addEventListener("pointermove", show);
      rect.addEventListener("pointerleave", hide);
      rect.addEventListener("focus", () => show());
      rect.addEventListener("blur", hide);
    });
  });
}

// -- table ------------------------------------------------------------------------

// columns: [{label, value: row => string, num: bool}]
export function renderTable(container, columns, rows) {
  container.replaceChildren();
  const wrap = htmlEl("div", { class: "table-wrap" }, container);
  const table = htmlEl("table", {}, wrap);
  const head = htmlEl("tr", {}, htmlEl("thead", {}, table));
  for (const c of columns) htmlEl("th", { class: c.num ? "num" : "", text: c.label }, head);
  const body = htmlEl("tbody", {}, table);
  for (const r of rows) {
    const tr = htmlEl("tr", {}, body);
    for (const c of columns) {
      const v = c.value(r);
      const td = htmlEl("td", { class: c.num ? "num" : "" }, tr);
      if (v instanceof Node) td.appendChild(v);
      else td.textContent = v === null || v === undefined ? "–" : String(v);
    }
  }
  if (!rows.length) {
    const tr = htmlEl("tr", {}, body);
    htmlEl("td", { colspan: columns.length, class: "muted", text: "No data yet" }, tr);
  }
}

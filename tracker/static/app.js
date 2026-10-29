import { columnChart, heatmap, hideTooltip, htmlEl, lineChart, renderTable, scatterChart } from "/static/charts.js";

const $ = (sel) => document.querySelector(sel);

const COLORS = {
  session: "var(--series-session)",
  week: "var(--series-week)",
  others: ["var(--series-3)", "var(--series-4)", "var(--series-5)"],
  maxed: "var(--ord-3)",
  partial: "var(--ord-2)",
  idle: "var(--ord-1)",
};

const WINDOW_LABELS = {
  five_hour: "5-hour session",
  seven_day: "Weekly limit",
  seven_day_opus: "Weekly · Opus",
  seven_day_sonnet: "Weekly · Sonnet",
  seven_day_oauth_apps: "Weekly · OAuth apps",
};

const state = {
  days: 30,
  status: null,
  data: null,
  lastPollTs: null,
  hidden: new Set(),
  tableView: new Set(),
  sessionsShown: 25,
};

// -- formatting ----------------------------------------------------------------

const pct = (v) => (v === null || v === undefined ? "–" : `${Math.round(v)}%`);
const num = (v, digits = 1) => (v === null || v === undefined ? "–" : `${+Number(v).toFixed(digits)}`);

function duration(seconds) {
  if (seconds === null || seconds === undefined) return "–";
  const s = Math.max(0, Math.round(seconds));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
}

function dateTime(ts, opts = {}) {
  if (!ts) return "–";
  return new Date(ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", ...opts });
}

function shortDate(ts) {
  return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function ago(ts, now) {
  if (!ts) return "never";
  const s = now - ts;
  if (s < 90) return "just now";
  return `${duration(s)} ago`;
}

function windowLabel(key) {
  if (WINDOW_LABELS[key]) return WINDOW_LABELS[key];
  if (key.startsWith("limits:")) {
    return key.split(":").slice(1).filter((p) => p && p !== "?").map((p) => p.replace(/_/g, " ")).join(" · ");
  }
  return key.replace(/_/g, " ");
}

function severity(util) {
  const threshold = state.status?.settings?.maxed_threshold ?? 99;
  if (util >= threshold) return { color: "var(--critical)", icon: "■", label: "Limit reached" };
  if (util >= 75) return { color: "var(--warning)", icon: "▲", label: "Getting close" };
  return { color: "var(--accent)", icon: "●", label: "Available" };
}

// -- API -------------------------------------------------------------------------

async function api(path, options = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
  return body;
}

function storage(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch {
    return null;
  }
  return null;
}

// -- header, banner, current usage -------------------------------------------------

function renderHeader() {
  const s = state.status;
  const pill = $("#poll-pill .dot");
  const text = $("#poll-text");
  const p = s.poller;
  pill.className = "dot";
  if (s.demo) {
    text.textContent = "Demo data";
    pill.classList.add("warn");
  } else if (!s.auth.connected) {
    text.textContent = "Not connected";
    pill.classList.add("bad");
  } else if (s.last_poll && !s.last_poll.ok) {
    text.textContent = `Last poll failed ${ago(s.last_poll.ts, s.now)}`;
    pill.classList.add("bad");
  } else {
    text.textContent = `Polling every ${duration(p.interval)} · updated ${ago(s.last_poll?.ts, s.now)}`;
    pill.classList.add("ok");
  }
  $("#poll-now").disabled = s.demo || !s.auth.connected;

  const banner = $("#banner");
  banner.replaceChildren();
  let message = null;
  let cls = "banner";
  if (s.demo) {
    message = "Demo mode: these numbers are simulated. Remove DEMO_MODE to track your real account.";
  } else if (!s.auth.connected) {
    message = "Connect your Claude account to start tracking. Readings are collected from then on.";
  } else if (s.last_poll && !s.last_poll.ok) {
    message = `The last poll failed: ${s.last_poll.error}`;
    cls += " error";
  }
  if (message) {
    const box = htmlEl("div", { class: cls }, banner);
    htmlEl("p", { text: message }, box);
    if (!s.demo) {
      const btn = htmlEl("button", { type: "button", class: "primary", text: s.auth.connected ? "Account" : "Connect" }, box);
      btn.addEventListener("click", openAccount);
    }
  }
}

function meterCard(el, title, reading, now, idleText) {
  el.replaceChildren();
  htmlEl("h2", { text: title }, el);
  const active = reading && reading.utilization !== null && reading.resets_at && reading.resets_at > now;
  const util = active ? reading.utilization : 0;
  const value = htmlEl("div", { class: "meter-value" }, el);
  value.append(pct(util));
  htmlEl("small", { text: "used" }, value);
  const sev = severity(util);
  const meter = htmlEl("div", { class: "meter", role: "meter", "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": Math.round(util), "aria-label": title }, el);
  meter.style.setProperty("--meter", sev.color);
  htmlEl("span", { style: { width: `${Math.min(100, util)}%` } }, meter);
  const foot = htmlEl("div", { class: "meter-foot" }, el);
  if (!reading) {
    htmlEl("span", { text: "No reading yet" }, foot);
    return;
  }
  const st = htmlEl("span", { class: "state" }, foot);
  htmlEl("span", { text: sev.icon, style: { color: sev.color }, "aria-hidden": "true" }, st);
  st.append(active ? sev.label : "Not started");
  htmlEl("span", {
    text: active ? `Resets in ${duration(reading.resets_at - now)} · ${dateTime(reading.resets_at, { weekday: "short" })}` : idleText,
  }, foot);
}

function renderCurrent() {
  const s = state.status;
  const now = s.now;
  const windows = s.current ? s.current.windows : [];
  const find = (k) => windows.find((w) => w.window === k);
  meterCard($("#card-session"), "5-hour session", find("five_hour"), now, "Starts with your next message");
  meterCard($("#card-week"), "Weekly limit", find("seven_day"), now, "Starts with your next message");

  const other = $("#card-other");
  other.replaceChildren();
  htmlEl("h2", { text: "Other limits" }, other);
  const rest = windows.filter((w) => w.window !== "five_hour" && w.window !== "seven_day" && w.utilization !== null);
  if (!rest.length) {
    htmlEl("p", { class: "sub", text: s.current ? "No other limits reported for this plan." : "No reading yet." }, other);
  } else {
    const list = htmlEl("ul", { class: "minor-list" }, other);
    for (const w of rest) {
      const li = htmlEl("li", {}, list);
      htmlEl("span", { class: "name", text: windowLabel(w.window), title: w.window }, li);
      const live = w.resets_at && w.resets_at > now;
      const util = live || !w.resets_at ? w.utilization : 0;
      htmlEl("strong", { text: pct(util) }, li);
      const meter = htmlEl("div", { class: "meter" }, li);
      meter.style.setProperty("--meter", severity(util).color);
      htmlEl("span", { style: { width: `${Math.min(100, util)}%` } }, meter);
    }
  }
  const extra = s.current && s.current.extra_usage;
  if (extra && extra.is_enabled) {
    const used = extra.used_credits != null ? (extra.used_credits / 100).toFixed(2) : "–";
    const limit = extra.monthly_limit != null ? (extra.monthly_limit / 100).toFixed(2) : "–";
    htmlEl("p", { class: "sub", text: `Extra usage this month: ${used} of ${limit} ${extra.currency || ""}` }, other);
  }
  if (s.current) {
    htmlEl("p", { class: "sub small", text: `As of ${dateTime(s.current.ts)}` }, other);
  }
}

// -- hero and tiles ------------------------------------------------------------------

function renderHero() {
  const hero = $("#hero");
  hero.replaceChildren();
  const est = state.data.summary.estimate;
  const all = est.all_sessions;
  htmlEl("div", { class: "figure", text: all && all.sessions_to_weekly ? num(all.sessions_to_weekly) : "–" }, hero);
  htmlEl("div", { class: "figure-label", text: "maxed-out 5-hour sessions fill your weekly limit" }, hero);
  if (all && all.full_session_cost) {
    htmlEl("p", {
      class: "figure-note",
      text:
        `One full 5-hour session uses about ${num(all.full_session_cost)}% of your weekly limit ` +
        `(middle half of sessions: ${num(all.p25_cost)}–${num(all.p75_cost)}%). ` +
        `Based on ${all.sessions} session${all.sessions === 1 ? "" : "s"} in this range, comparing how much of each limit they used.`,
    }, hero);
  } else {
    htmlEl("p", {
      class: "figure-note",
      text: "Not enough data yet. This needs at least one 5-hour session that used 10% or more of its limit while the tracker was watching.",
    }, hero);
  }
  const dl = htmlEl("dl", {}, hero);
  const add = (term, value) => {
    const wrap = htmlEl("div", {}, dl);
    htmlEl("dt", { text: term }, wrap);
    htmlEl("dd", { text: value }, wrap);
  };
  const maxed = est.maxed_sessions;
  add("From maxed-out sessions only", maxed ? `${num(maxed.sessions_to_weekly)} sessions (${maxed.sessions} observed)` : "No maxed sessions yet");
  const obs = est.observed_weeks;
  add(
    "Weeks you actually hit the limit",
    obs.weeks
      ? `${num(obs.avg_sessions)} sessions, ${num(obs.avg_session_equivalents)} full-session equivalents (${obs.weeks} week${obs.weeks === 1 ? "" : "s"})`
      : "None in this range"
  );
}

function renderTiles() {
  const { sessions: s, weeks: w } = state.data.summary;
  const tiles = $("#tiles");
  tiles.replaceChildren();
  const tile = (label, value, note) => {
    const t = htmlEl("div", { class: "card tile" }, tiles);
    htmlEl("div", { class: "label", text: label }, t);
    htmlEl("div", { class: "value", text: value }, t);
    if (note) htmlEl("div", { class: "note", text: note }, t);
  };
  tile("5-hour sessions used", String(s.used), `of ${s.max_possible} possible back-to-back`);
  tile("Maxed-out sessions", String(s.maxed), s.used ? `${Math.round((100 * s.maxed) / s.used)}% of sessions used` : null);
  tile("Idle 5-hour slots", String(s.idle_slots), "5-hour blocks with no session");
  tile("Average session peak", pct(s.avg_peak), s.median_peak !== null ? `median ${pct(s.median_peak)}` : null);
  tile("Unused in started sessions", num(s.unused_capacity), "sessions' worth of capacity");
  tile("Weeks at the limit", `${w.maxed} of ${w.completed}`, "completed weekly periods");
  tile("Unused weekly capacity", `${num(w.unused_capacity)} wk`, w.avg_final !== null ? `average ${pct(w.avg_final)} used at reset` : null);
  tile("Locked out", `${num(s.locked_hours)} h`, `by the 5-hour limit · ${num(w.locked_hours)} h weekly`);
}

// -- chart cards --------------------------------------------------------------------

function chartCard(el, { id, title, sub, legend, draw, table }) {
  el.replaceChildren();
  const head = htmlEl("div", { class: "card-head" }, el);
  const titles = htmlEl("div", {}, head);
  htmlEl("h2", { text: title }, titles);
  if (sub) htmlEl("p", { class: "sub", text: sub }, titles);
  const showTable = state.tableView.has(id);
  const toggle = htmlEl("button", { type: "button", "aria-pressed": String(showTable), text: showTable ? "Chart" : "Table" }, head);
  toggle.addEventListener("click", () => {
    if (state.tableView.has(id)) state.tableView.delete(id);
    else state.tableView.add(id);
    hideTooltip();
    chartCard(el, { id, title, sub, legend, draw, table });
  });
  const body = htmlEl("div", {}, el);
  if (showTable) {
    renderTable(body, table.columns, table.rows);
    return;
  }
  if (legend && legend.length) {
    const ul = htmlEl("ul", { class: "legend" }, body);
    for (const item of legend) {
      const li = htmlEl("li", {}, ul);
      const target = item.toggle ? htmlEl("button", { type: "button", "aria-pressed": String(!item.hidden) }, li) : li;
      htmlEl("span", { class: item.kind === "rect" ? "key-rect" : item.kind === "band" ? "key-rect key-blind" : "key-line", style: item.color ? { background: item.color } : {} }, target);
      target.append(item.label);
      if (item.toggle) target.addEventListener("click", item.toggle);
    }
  }
  const chart = htmlEl("div", { class: "chart" }, body);
  draw(chart);
}

function renderTimeline() {
  const d = state.data;
  const keys = Object.keys(d.timeseries);
  let other = 0;
  const series = keys.map((k) => ({
    key: k,
    label: windowLabel(k),
    color: k === "five_hour" ? COLORS.session : k === "seven_day" ? COLORS.week : COLORS.others[other++ % COLORS.others.length],
    points: d.timeseries[k],
    hidden: state.hidden.has(k) || (!state.hidden.has(`show:${k}`) && k !== "five_hour" && k !== "seven_day"),
  }));
  const start = d.summary.range.start;
  const legend = series.map((s) => ({
    label: s.label,
    color: s.color,
    kind: "line",
    hidden: s.hidden,
    toggle: () => {
      const primary = s.key === "five_hour" || s.key === "seven_day";
      if (primary) state.hidden.has(s.key) ? state.hidden.delete(s.key) : state.hidden.add(s.key);
      else state.hidden.has(`show:${s.key}`) ? state.hidden.delete(`show:${s.key}`) : state.hidden.add(`show:${s.key}`);
      renderTimeline();
    },
  }));
  if (d.blind_spots.length) legend.push({ label: "Tracker offline", kind: "band" });
  const rows = [];
  const visible = series.filter((s) => !s.hidden);
  const byTs = new Map();
  for (const s of visible) {
    for (const [t, v] of s.points) {
      if (v === null) continue;
      if (!byTs.has(t)) byTs.set(t, {});
      byTs.get(t)[s.key] = v;
    }
  }
  for (const [t, vals] of [...byTs.entries()].sort((a, b) => b[0] - a[0])) rows.push({ t, ...vals });
  chartCard($("#chart-timeline"), {
    id: "timeline",
    title: "Usage over time",
    sub: "How much of each limit was used, as reported by Claude. Click a legend item to show or hide it.",
    legend,
    draw: (el) =>
      lineChart(el, {
        series,
        domain: [start, d.now],
        yMax: 100,
        height: 280,
        bands: d.blind_spots,
        endLabels: true,
        ariaLabel: "Line chart of 5-hour and weekly limit utilization over time",
        empty: "No readings in this range yet",
      }),
    table: {
      columns: [{ label: "Time", value: (r) => dateTime(r.t) }].concat(visible.map((s) => ({ label: s.label, num: true, value: (r) => pct(r[s.key]) }))),
      rows,
    },
  });
}

function weekTitle(w) {
  return `${shortDate(w.start)} – ${shortDate(w.end)}${w.active ? " (in progress)" : ""}`;
}

function renderWeekCharts() {
  const weeks = [...state.data.weeks].reverse();
  const stack = [
    { key: "maxed", label: "Maxed out", color: COLORS.maxed },
    { key: "partial", label: "Partly used", color: COLORS.partial },
    { key: "idle", label: "Idle slots", color: COLORS.idle },
  ];
  chartCard($("#chart-weeks"), {
    id: "weeks",
    title: "5-hour sessions per weekly period",
    sub: "Sessions you maxed out, sessions you started but didn't max, and 5-hour slots left idle.",
    legend: stack.map((s) => ({ label: s.label, color: s.color, kind: "rect" })),
    draw: (el) =>
      columnChart(el, {
        categories: weeks.map((w) => ({
          label: shortDate(w.start),
          title: weekTitle(w),
          values: { maxed: w.sessions_maxed, partial: w.sessions_partial, idle: w.idle_slots },
          extraRows: [{ value: pct(w.peak), label: "of weekly limit used" }],
        })),
        stack,
        valueFormat: (v) => `${Math.round(v)}`,
        ariaLabel: "Stacked columns of maxed, partly used and idle 5-hour sessions per weekly period",
        empty: "No weekly periods in this range yet",
      }),
    table: {
      columns: [
        { label: "Week", value: weekTitle },
        { label: "Maxed out", num: true, value: (w) => w.sessions_maxed },
        { label: "Partly used", num: true, value: (w) => w.sessions_partial },
        { label: "Idle slots", num: true, value: (w) => w.idle_slots },
      ],
      rows: [...state.data.weeks],
    },
  });

  chartCard($("#chart-weekly-final"), {
    id: "weekly-final",
    title: "Weekly limit used per period",
    sub: "How much of each weekly limit was used by the time it reset. Below the line is capacity you didn't use.",
    draw: (el) =>
      columnChart(el, {
        categories: weeks.map((w) => ({
          label: shortDate(w.start),
          title: weekTitle(w),
          values: { used: w.peak },
          extraRows: [{ value: String(w.sessions_used), label: "sessions" }],
        })),
        stack: [{ key: "used", label: "of weekly limit used", color: COLORS.week }],
        yMax: 100,
        yFormat: (v) => `${v}%`,
        valueFormat: (v) => `${Math.round(v)}%`,
        capLabels: true,
        refLine: { value: 100, label: "Limit" },
        ariaLabel: "Columns of weekly limit utilization at reset for each weekly period",
        empty: "No weekly periods in this range yet",
      }),
    table: {
      columns: [
        { label: "Week", value: weekTitle },
        { label: "Used", num: true, value: (w) => pct(w.peak) },
        { label: "Unused", num: true, value: (w) => (w.unused === null ? "–" : pct(w.unused)) },
        { label: "Hit limit", value: (w) => (w.maxed ? "Yes" : "No") },
      ],
      rows: [...state.data.weeks],
    },
  });
}

function renderCostCharts() {
  const d = state.data;
  const est = d.summary.estimate.all_sessions;
  const points = d.sessions
    .filter((s) => s.full_session_cost !== null)
    .map((s) => ({
      x: s.session_delta,
      y: s.weekly_delta,
      title: dateTime(s.start, { weekday: "short" }),
      rows: [
        { color: COLORS.session, value: pct(s.session_delta), label: "of the 5-hour limit" },
        { value: `${num(s.weekly_delta)}%`, label: "of the weekly limit" },
        { value: `${num(s.full_session_cost)}%`, label: "weekly per full session" },
      ],
    }));
  chartCard($("#chart-cost"), {
    id: "cost",
    title: "Weekly cost of each session",
    sub: "Each dot is a 5-hour session. The line is the average rate across all of them.",
    draw: (el) =>
      scatterChart(el, {
        points,
        xMax: 100,
        color: COLORS.session,
        fit: est && est.full_session_cost ? { slope: est.full_session_cost / 100, label: `≈ ${num(est.full_session_cost)}% per full session` } : null,
        xLabel: "5-hour limit used in the session",
        yLabel: "Weekly limit used",
        height: 280,
        ariaLabel: "Scatter plot of 5-hour limit used versus weekly limit used per session",
        empty: "No sessions with enough usage yet",
      }),
    table: {
      columns: [
        { label: "Session start", value: (s) => dateTime(s.start) },
        { label: "5-hour used", num: true, value: (s) => pct(s.session_delta) },
        { label: "Weekly used", num: true, value: (s) => `${num(s.weekly_delta)}%` },
        { label: "Weekly per full session", num: true, value: (s) => `${num(s.full_session_cost)}%` },
      ],
      rows: d.sessions.filter((s) => s.full_session_cost !== null),
    },
  });

  const hist = d.summary.histogram;
  chartCard($("#chart-peaks"), {
    id: "peaks",
    title: "How far sessions got",
    sub: "Peak 5-hour utilization reached by each session.",
    draw: (el) =>
      columnChart(el, {
        categories: hist.map((b) => ({ label: b.hi === null ? "Max" : `${b.lo}`, title: b.hi === null ? "Maxed out" : `${b.lo}–${b.hi}% peak`, values: { n: b.count } })),
        stack: [{ key: "n", label: "sessions", color: COLORS.session }],
        valueFormat: (v) => `${Math.round(v)}`,
        capLabels: true,
        height: 280,
        ariaLabel: "Histogram of peak 5-hour utilization per session",
        empty: "No sessions yet",
      }),
    table: {
      columns: [
        { label: "Peak", value: (b) => (b.hi === null ? "Maxed out" : `${b.lo}–${b.hi}%`) },
        { label: "Sessions", num: true, value: (b) => b.count },
      ],
      rows: hist,
    },
  });
}

function renderHeatmap() {
  const days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const values = days.map(() => Array(24).fill(0));
  for (const s of state.data.sessions) {
    const t = new Date(s.start * 1000);
    values[(t.getDay() + 6) % 7][t.getHours()] += 1;
  }
  const hourLabel = (h) => new Date(2000, 0, 1, h).toLocaleTimeString(undefined, { hour: "numeric" });
  const rows = [];
  values.forEach((r, i) => r.forEach((v, h) => v && rows.push({ day: days[i], hour: h, v })));
  chartCard($("#chart-heatmap"), {
    id: "heatmap",
    title: "When your 5-hour sessions start",
    sub: "Session starts by weekday and hour, in your local time. Darker means more sessions.",
    draw: (el) =>
      heatmap(el, {
        rows: days,
        cols: Array.from({ length: 24 }, (_, h) => hourLabel(h)),
        values,
        steps: Array.from({ length: 8 }, (_, i) => `var(--seq-${i})`),
        valueLabel: "sessions started",
        title: (i, h) => `${days[i]} ${hourLabel(h)}–${hourLabel((h + 1) % 24)}`,
        ariaLabel: "Heatmap of session start times by weekday and hour",
      }),
    table: {
      columns: [
        { label: "Day", value: (r) => r.day },
        { label: "Hour", value: (r) => hourLabel(r.hour) },
        { label: "Sessions", num: true, value: (r) => r.v },
      ],
      rows,
    },
  });
}

// -- tables ----------------------------------------------------------------------------

function statusTag(on, onText, offText) {
  const span = htmlEl("span", { class: "tag" });
  if (on) {
    htmlEl("span", { text: "■", style: { color: "var(--critical)" }, "aria-hidden": "true" }, span);
    span.append(onText);
  } else {
    span.append(offText);
  }
  return span;
}

function renderTables() {
  const weeksEl = $("#table-weeks");
  weeksEl.replaceChildren();
  const wh = htmlEl("div", { class: "card-head" }, weeksEl);
  const wt = htmlEl("div", {}, wh);
  htmlEl("h2", { text: "Weekly periods" }, wt);
  htmlEl("p", { class: "sub", text: "Each weekly limit period, how much of it you used, and what it took to reach the limit." }, wt);
  const wbody = htmlEl("div", {}, weeksEl);
  renderTable(wbody, [
    { label: "Period", value: weekTitle },
    { label: "Used", num: true, value: (w) => pct(w.peak) },
    { label: "Limit", value: (w) => statusTag(w.maxed, `Hit after ${num(w.to_limit?.hours_after_start)} h`, w.active ? "In progress" : "Not reached") },
    { label: "Sessions", num: true, value: (w) => w.sessions_used },
    { label: "Maxed", num: true, value: (w) => w.sessions_maxed },
    { label: "Idle slots", num: true, value: (w) => w.idle_slots },
    { label: "Full-session equiv.", num: true, value: (w) => num(w.session_equivalents) },
    { label: "Sessions to limit", num: true, value: (w) => (w.to_limit ? w.to_limit.sessions : "–") },
    { label: "Locked out", num: true, value: (w) => (w.locked_hours ? `${num(w.locked_hours)} h` : "–") },
  ], state.data.weeks);

  const el = $("#table-sessions");
  el.replaceChildren();
  const head = htmlEl("div", { class: "card-head" }, el);
  const t = htmlEl("div", {}, head);
  htmlEl("h2", { text: "5-hour sessions" }, t);
  htmlEl("p", { class: "sub", text: "Newest first. Weekly figures need the start of the session to have been observed." }, t);
  const body = htmlEl("div", {}, el);
  const sessions = state.data.sessions;
  renderTable(body, [
    { label: "Started", value: (s) => dateTime(s.start, { weekday: "short" }) },
    { label: "Peak", num: true, value: (s) => pct(s.peak) },
    { label: "Status", value: (s) => (s.active ? "Active" : statusTag(s.maxed, "Maxed out", "Partly used")) },
    { label: "Locked out", num: true, value: (s) => (s.locked_minutes ? duration(s.locked_minutes * 60) : "–") },
    { label: "Weekly used", num: true, value: (s) => (s.weekly_delta === null ? "–" : `${num(s.weekly_delta)}%`) },
    { label: "Weekly per full session", num: true, value: (s) => (s.full_session_cost === null ? "–" : `${num(s.full_session_cost)}%`) },
  ], sessions.slice(0, state.sessionsShown));
  const foot = htmlEl("div", { class: "table-foot" }, el);
  htmlEl("span", { text: `Showing ${Math.min(state.sessionsShown, sessions.length)} of ${sessions.length}` }, foot);
  if (sessions.length > state.sessionsShown) {
    const more = htmlEl("button", { type: "button", text: "Show more" }, foot);
    more.addEventListener("click", () => {
      state.sessionsShown += 50;
      renderTables();
    });
  }
}

function renderCoverage() {
  const c = state.data.summary.coverage;
  $("#coverage").textContent = c.first_poll
    ? `Tracking since ${dateTime(c.first_poll)} · ${num(c.watched_hours, 0)} h watched${c.blind_hours ? ` · ${num(c.blind_hours, 0)} h offline` : ""}`
    : "";
  const s = state.status;
  $("#footer-info").textContent = `v${s.version} · ${s.polls.ok} successful polls · a session counts as maxed at ${s.settings.maxed_threshold}%`;
}

// -- loading -----------------------------------------------------------------------------

async function loadStatus() {
  state.status = await api("/api/status");
  renderHeader();
  renderCurrent();
  const ts = state.status.last_poll?.ts ?? null;
  if (ts !== state.lastPollTs) {
    state.lastPollTs = ts;
    await loadDashboard();
  }
}

async function loadDashboard() {
  $("#app").classList.add("loading");
  try {
    state.data = await api(`/api/dashboard?days=${state.days}`);
  } finally {
    $("#app").classList.remove("loading");
  }
  renderHero();
  renderTiles();
  renderTimeline();
  renderWeekCharts();
  renderCostCharts();
  renderHeatmap();
  renderTables();
  renderCoverage();
}

function setupRange() {
  const saved = Number(storage("range"));
  if ([0, 7, 30, 90].includes(saved) && storage("range") !== null) state.days = saved;
  const buttons = document.querySelectorAll("#range button");
  const sync = () => buttons.forEach((b) => b.setAttribute("aria-pressed", String(Number(b.dataset.days) === state.days)));
  buttons.forEach((b) =>
    b.addEventListener("click", () => {
      state.days = Number(b.dataset.days);
      state.sessionsShown = 25;
      storage("range", String(state.days));
      sync();
      loadDashboard().catch(showError);
    })
  );
  sync();
}

function showError(err) {
  const banner = $("#banner");
  banner.replaceChildren();
  const box = htmlEl("div", { class: "banner error" }, banner);
  htmlEl("p", { text: `Couldn't load data: ${err.message}` }, box);
}

// -- account dialog ------------------------------------------------------------------------

function renderAccountStatus() {
  const a = state.status?.auth;
  const el = $("#account-status");
  if (!a) return;
  if (!a.connected) {
    el.textContent = a.error ? `Not connected. ${a.error}` : "Not connected.";
    return;
  }
  const source = { signin: "signed in through the tracker", import: "imported from Claude Code", file: "read from CLAUDE_CREDENTIALS_FILE", env: "from CLAUDE_OAUTH_TOKEN" }[a.source] || a.source;
  const parts = [`Connected, ${source}`];
  if (a.account) parts.push(a.account);
  parts.push(a.can_refresh ? "refreshes automatically" : a.expires_at ? `token expires ${dateTime(a.expires_at)}` : "token is not refreshed");
  el.textContent = parts.join(" · ") + (a.error ? `. Last error: ${a.error}` : ".");
}

function openAccount() {
  renderAccountStatus();
  $("#signin-msg").textContent = "";
  $("#import-msg").textContent = "";
  $("#account").showModal();
}

function setMsg(el, text, ok) {
  el.textContent = text;
  el.className = `msg ${ok ? "ok" : "error"}`;
}

function setupAccount() {
  $("#account-btn").addEventListener("click", openAccount);
  $("#account-close").addEventListener("click", () => $("#account").close());
  let pendingState = null;
  $("#open-signin").addEventListener("click", async () => {
    // Open the tab synchronously so pop-up blockers allow it, then point it at the URL.
    const tab = window.open("about:blank", "_blank");
    try {
      const res = await api("/api/auth/login", { method: "POST" });
      pendingState = res.state;
      if (tab) {
        tab.opener = null;
        tab.location.href = res.url;
      } else {
        window.location.href = res.url;
      }
      setMsg($("#signin-msg"), "Waiting for the code from the sign-in page…", true);
    } catch (err) {
      if (tab) tab.close();
      setMsg($("#signin-msg"), err.message, false);
    }
  });
  $("#signin-submit").addEventListener("click", async () => {
    const code = $("#signin-code").value.trim();
    if (!code) return setMsg($("#signin-msg"), "Paste the code first.", false);
    try {
      await api("/api/auth/complete", { method: "POST", body: JSON.stringify({ code, state: pendingState }) });
      $("#signin-code").value = "";
      setMsg($("#signin-msg"), "Connected. The first reading will appear shortly.", true);
      await refreshSoon();
    } catch (err) {
      setMsg($("#signin-msg"), err.message, false);
    }
  });
  $("#import-submit").addEventListener("click", async () => {
    const text = $("#import-text").value.trim();
    if (!text) return setMsg($("#import-msg"), "Paste the credentials first.", false);
    try {
      await api("/api/auth/import", { method: "POST", body: JSON.stringify({ text }) });
      $("#import-text").value = "";
      setMsg($("#import-msg"), "Imported. The first reading will appear shortly.", true);
      await refreshSoon();
    } catch (err) {
      setMsg($("#import-msg"), err.message, false);
    }
  });
  $("#disconnect").addEventListener("click", async () => {
    if (!confirm("Disconnect this Claude account? Collected history is kept.")) return;
    await api("/api/auth/logout", { method: "POST" });
    await loadStatus();
    renderAccountStatus();
  });
  $("#poll-now").addEventListener("click", async () => {
    $("#poll-now").disabled = true;
    try {
      await api("/api/poll", { method: "POST" });
    } catch (err) {
      showError(err);
    }
    await loadStatus().catch(showError);
  });
}

async function refreshSoon() {
  await loadStatus();
  renderAccountStatus();
  setTimeout(() => loadStatus().then(renderAccountStatus).catch(() => {}), 4000);
}

setupRange();
setupAccount();
loadStatus().catch(showError);
setInterval(() => loadStatus().catch(showError), 30000);

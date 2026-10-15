"use strict";

/* ------------------------------------------------------------ helpers */

const $ = (sel) => document.querySelector(sel);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const isNum = (v) => typeof v === "number" && isFinite(v);

function fmt(v, digits) {
  return isNum(v) ? v.toFixed(digits) : "—";
}

/* Animate a number from its current value to a new one (quick, no bounce). */
const tweens = new WeakMap();
function tweenText(el, target, digits) {
  if (!isNum(target)) { el.textContent = "—"; tweens.delete(el); return; }
  const from = tweens.get(el);
  if (!isNum(from) || Math.abs(target - from) < Math.pow(10, -digits)) {
    el.textContent = target.toFixed(digits);
    tweens.set(el, target);
    return;
  }
  const start = performance.now();
  const dur = 280;
  tweens.set(el, target);
  const step = (now) => {
    if (tweens.get(el) !== target) return;      // a newer value took over
    const p = Math.min(1, (now - start) / dur);
    const e = 1 - Math.pow(1 - p, 3);
    el.textContent = (from + (target - from) * e).toFixed(digits);
    if (p < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

/* ------------------------------------------------------------ metric cards */

const METRICS = [
  { key: "loss", label: "Packet loss", unit: "%", digits: 1,
    get: (s) => s.rx.loss_pct,
    hint: "Packets that never arrived, over the last 3 seconds." },
  { key: "r", label: "Redundancy", unit: "", digits: 0,
    get: (s) => s.rx.r,
    unitFn: (s) => isNum(s.rx.k) ? `parity / ${s.rx.k} data` : "",
    hint: "Parity packets r the sender adds to each k-packet frame." },
  { key: "overhead", label: "FEC overhead", unit: "%", digits: 0,
    get: (s) => s.rx.overhead_pct,
    hint: "Extra bandwidth spent on parity (r ÷ k)." },
  { key: "recovery", label: "Frames recovered", unit: "%", digits: 0,
    get: (s) => s.rx.recovery_pct,
    tone: (v) => v >= 99 ? "good" : v >= 90 ? "warn" : "bad",
    hint: "Frames shown fully intact, with or without repair." },
  { key: "rescued", label: "Rebuilt by FEC", unit: "%", digits: 0,
    get: (s) => s.rx.rescued_pct,
    hint: "Frames that lost packets yet were reconstructed." },
  { key: "psnr", label: "PSNR", unit: "dB", digits: 1,
    get: (s) => s.rx.psnr,
    tone: (v) => v >= 35 ? "good" : v >= 25 ? "warn" : "bad",
    hint: "Fidelity vs the original frame. Above 35 dB looks clean." },
  { key: "ssim", label: "SSIM", unit: "", digits: 3,
    get: (s) => s.rx.ssim,
    hint: "Structural similarity to the original; 1.000 is identical." },
  { key: "latency", label: "Latency", unit: "ms", digits: 0,
    get: (s) => s.rx.latency_ms,
    hint: "Capture to decoded frame, clock offset corrected." },
];

function buildMetricCards() {
  const root = $("#metrics");
  for (const m of METRICS) {
    const card = document.createElement("div");
    card.className = "card metric";
    card.innerHTML =
      `<div class="label">${m.label}</div>` +
      `<div class="value"><span class="num">—</span><span class="unit">${m.unit}</span></div>` +
      `<div class="hint">${m.hint}</div>`;
    m.num = card.querySelector(".num");
    m.unitEl = card.querySelector(".unit");
    m.valueEl = card.querySelector(".value");
    root.appendChild(card);
  }
}

function renderMetrics(s) {
  for (const m of METRICS) {
    const v = s.rx.active ? m.get(s) : null;
    tweenText(m.num, v, m.digits);
    if (m.unitFn) m.unitEl.textContent = s.rx.active ? m.unitFn(s) : "";
    m.unitEl.hidden = !isNum(v);
    if (m.tone && isNum(v)) m.valueEl.dataset.tone = m.tone(v);
    else delete m.valueEl.dataset.tone;
  }
  const psnr = METRICS.find((m) => m.key === "psnr");
  if (s.rx.active && !isNum(s.rx.psnr)) {
    psnr.valueEl.nextElementSibling.textContent = "Needs a shared clip (--mode file) for ground truth.";
  }
}

/* ------------------------------------------------------------ explainer */

const LEVELS = {
  idle: { label: "Waiting", icon: "…" },
  ok:   { label: "Stable", icon: "✓" },
  warn: { label: "At FEC budget", icon: "!" },
  bad:  { label: "Frames lost", icon: "✕" },
};

function renderExplain(s) {
  const e = s.explain;
  const card = $("#explain");
  card.dataset.level = e.level;
  $("#status-icon").textContent = LEVELS[e.level].icon;
  $("#status-label").textContent = LEVELS[e.level].label;
  if ($("#explain-title").textContent !== e.title) $("#explain-title").textContent = e.title;
  if ($("#explain-body").textContent !== e.body) $("#explain-body").textContent = e.body;

  renderOutcomes(s.rx.outcomes || {});

  const rx = s.rx, tx = s.tx;
  $("#fact-in").textContent = rx.active
    ? `${s.mode_name} · ${fmt(rx.fps, 0)} fps`
    : "—";
  $("#fact-out").textContent = isNum(tx.k)
    ? `${modeName(s.config.fec)} · r = ${tx.r} / k = ${tx.k}` +
      (isNum(tx.kbps) ? ` · ${(tx.kbps / 1000).toFixed(1)} Mbit/s` : "") +
      (isNum(tx.peer_reported_loss_pct) ? ` · peer sees ${fmt(tx.peer_reported_loss_pct, 1)}% loss` : "")
    : "Not sending";
  $("#fact-peer").textContent = s.config.peer + (isNum(s.rtt_ms) ? ` · RTT ${fmt(s.rtt_ms, 1)} ms` : "");

  const badge = $("#mode-badge");
  const mode = rx.mode || s.config.fec;
  badge.dataset.mode = mode;
  badge.textContent = modeName(mode);
  const pill = $("#link-pill");
  pill.dataset.live = String(rx.active);
  $("#link-text").textContent = rx.active ? "Receiving" : "No video";
}

const OUTCOMES = [
  { key: "intact", label: "Arrived intact" },
  { key: "rescued", label: "Rebuilt by FEC" },
  { key: "damaged", label: "Shown damaged" },
  { key: "lost", label: "Lost, frozen" },
];

function renderOutcomes(counts) {
  const bar = $("#outcome-bar"), legend = $("#outcome-legend");
  if (!bar.children.length) {
    bar.innerHTML = OUTCOMES.map((o) => `<span class="o-${o.key}" style="background: var(--c); flex: 0 0 0"></span>`).join("");
    legend.innerHTML = OUTCOMES.map((o) => `<li class="o-${o.key}"><i></i>${o.label}<b>—</b></li>`).join("");
  }
  OUTCOMES.forEach((o, i) => {
    const n = counts[o.key] || 0;
    bar.children[i].style.flex = `${n} 0 0`;
    bar.children[i].style.display = n ? "block" : "none";
    legend.children[i].querySelector("b").textContent = String(n);
  });
  bar.setAttribute("aria-label", "Frame outcomes: " +
    OUTCOMES.map((o) => `${counts[o.key] || 0} ${o.label.toLowerCase()}`).join(", "));
}

function modeName(m) {
  return { off: "No FEC", fixed: "Fixed FEC", adaptive: "Adaptive FEC" }[m] || "—";
}

/* ------------------------------------------------------------ charts */

let lossChart = null, psnrChart = null;
const resultCharts = [];

function baseOptions(ySuffix, yMax) {
  const text2 = css("--text-2"), grid = css("--grid");
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: css("--surface"),
        titleColor: css("--text"),
        bodyColor: css("--text-2"),
        borderColor: css("--hairline"),
        borderWidth: 1,
        padding: 10,
        cornerRadius: 10,
        boxPadding: 4,
        usePointStyle: true,
        callbacks: {},
      },
    },
    scales: {
      x: {
        grid: { display: false },
        border: { color: grid },
        ticks: { color: text2, maxTicksLimit: 7, font: { size: 11 } },
      },
      y: {
        min: 0,
        suggestedMax: yMax,
        grid: { color: grid },
        border: { display: false },
        ticks: { color: text2, font: { size: 11 }, maxTicksLimit: 6,
                 callback: (v) => `${Math.round(v * 10) / 10}${ySuffix}` },
      },
    },
  };
}

function line(label, colorVar, extra = {}) {
  const c = css(colorVar);
  return Object.assign({
    label, data: [], borderColor: c, backgroundColor: c,
    borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, tension: 0.3,
    spanGaps: true,
  }, extra);
}

function buildLiveCharts() {
  if (!window.Chart) return;
  Chart.defaults.font.family = css("--font");
  const lossOpts = baseOptions("%", 40);
  lossOpts.scales.x.ticks.callback = function (v) { return `${this.getLabelForValue(v)}s`; };
  lossOpts.plugins.tooltip.callbacks.title = (items) => `t = ${items[0].label} s`;
  lossOpts.plugins.tooltip.callbacks.label = (ctx) =>
    ` ${ctx.dataset.label}: ${fmt(ctx.parsed.y, 1)}%`;
  lossChart = new Chart($("#chart-loss"), {
    type: "line",
    data: { labels: [], datasets: [line("Packet loss", "--series-2"),
                                   line("Redundancy (r / k)", "--series-1")] },
    options: lossOpts,
  });
  const psnrOpts = baseOptions(" dB", 45);
  psnrOpts.scales.x.ticks.callback = lossOpts.scales.x.ticks.callback;
  psnrOpts.plugins.tooltip.callbacks.title = lossOpts.plugins.tooltip.callbacks.title;
  psnrOpts.plugins.tooltip.callbacks.label = (ctx) => ` PSNR: ${fmt(ctx.parsed.y, 1)} dB`;
  psnrChart = new Chart($("#chart-psnr"), {
    type: "line",
    data: { labels: [], datasets: [line("PSNR", "--series-1")] },
    options: psnrOpts,
  });
}

function renderCharts(s) {
  if (!lossChart) return;
  const h = s.history;
  const labels = h.map((p) => p.t.toFixed(0));
  lossChart.data.labels = labels;
  lossChart.data.datasets[0].data = h.map((p) => p.loss);
  lossChart.data.datasets[1].data = h.map((p) => p.overhead);
  lossChart.update("none");
  psnrChart.data.labels = labels;
  psnrChart.data.datasets[0].data = h.map((p) => p.psnr);
  psnrChart.update("none");
}

/* ------------------------------------------------------------ live data */

let lastSnapshot = null;

function render(s) {
  lastSnapshot = s;
  renderExplain(s);
  renderMetrics(s);
  renderCharts(s);
}

function connect() {
  let opened = false;
  let ws;
  try {
    ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  } catch (e) {
    return poll();
  }
  ws.onopen = () => { opened = true; };
  ws.onmessage = (ev) => render(JSON.parse(ev.data));
  ws.onclose = () => {
    if (!opened) return poll();          // WebSocket unsupported: fall back
    setTimeout(connect, 1000);           // dropped: retry
    markOffline();
  };
}

function poll() {
  fetch("/stats").then((r) => r.json()).then(render).catch(markOffline)
    .finally(() => setTimeout(poll, 333));
}

function markOffline() {
  $("#link-pill").dataset.live = "false";
  $("#link-text").textContent = "Dashboard offline";
}

/* ------------------------------------------------------------ video toggle */

function setupFeedToggle() {
  for (const b of document.querySelectorAll(".mini-seg button")) {
    b.addEventListener("click", () => {
      for (const o of document.querySelectorAll(".mini-seg button")) {
        o.setAttribute("aria-pressed", String(o === b));
      }
      const remote = b.dataset.feed === "remote";
      $("#video").src = remote ? "/video.mjpg" : "/local.mjpg";
      $(".video-card h2").textContent = remote ? "Received video" : "Your camera";
      $("#video-caption").textContent = remote
        ? "Decoded by the Python receiver after FEC repair, streamed here as MJPEG."
        : "Your own outgoing frames, before they are split into packets.";
    });
  }
}

/* ------------------------------------------------------------ results view */

const MODE_ORDER = ["off", "fixed", "adaptive"];
const MODE_COLOR = { off: "--series-3", fixed: "--series-2", adaptive: "--series-1" };

let resultsLoaded = false;

function loadResults() {
  fetch("/results/summary.json", { cache: "no-store" })
    .then((r) => { if (!r.ok) throw new Error("none"); return r.json(); })
    .then(renderResults)
    .catch(() => { $("#results-empty").hidden = false; $("#results-body").hidden = true; });
}

function rowFor(summary, loss, mode) {
  return summary.rows.find((r) => r.loss === loss && r.mode === mode);
}

function renderResults(summary) {
  resultsLoaded = true;
  $("#results-empty").hidden = true;
  $("#results-body").hidden = false;
  const levels = summary.loss_levels;
  const imp = summary.impairment === "netem" ? "tc netem on the loopback link" : "in-app packet dropping";
  $("#results-meta").textContent =
    `${levels.length} loss levels × 3 modes, ${summary.duration_s} s per run, loss injected with ${imp}. ` +
    `Fixed FEC uses r = ${summary.fixed_redundancy}. Generated ${summary.generated}.`;

  // Headlines at the 20% point (or the closest level).
  const ref = levels.reduce((a, b) => Math.abs(b - 20) < Math.abs(a - 20) ? b : a);
  const off = rowFor(summary, ref, "off"), fix = rowFor(summary, ref, "fixed"), ada = rowFor(summary, ref, "adaptive");
  const worst = levels[levels.length - 1];
  const wOff = rowFor(summary, worst, "off"), wFix = rowFor(summary, worst, "fixed"), wAda = rowFor(summary, worst, "adaptive");
  const heads = [
    { label: `Frames recovered at ${ref}% loss`,
      value: `${fmt(ada.recovery_pct, 1).replace(".0", "")}%`,
      hint: `Adaptive FEC, versus ${fmt(fix.recovery_pct, 1).replace(".0", "")}% fixed and ${fmt(off.recovery_pct, 1).replace(".0", "")}% without FEC.` },
    { label: `Picture quality at ${ref}% loss`,
      value: `${fmt(ada.psnr, 1)} dB`,
      hint: `Adaptive FEC, versus ${fmt(fix.psnr, 1)} dB fixed and ${fmt(off.psnr, 1)} dB without FEC.` },
    { label: `Worst case: ${worst}% loss`,
      value: `${fmt(wAda.recovery_pct, 1).replace(".0", "")}% recovered`,
      hint: `Adaptive FEC within its bitrate cap, versus ${fmt(wFix.recovery_pct, 1).replace(".0", "")}% fixed and ${fmt(wOff.recovery_pct, 1).replace(".0", "")}% without FEC.` },
  ];
  $("#results-headlines").innerHTML = heads.map((h) =>
    `<div class="headline"><div class="label">${h.label}</div><div class="value">${h.value}</div><div class="hint">${h.hint}</div></div>`
  ).join("");

  // Charts
  resultCharts.splice(0).forEach((c) => c.destroy());
  if (window.Chart) {
    const make = (id, key, suffix, yMax, digits) => {
      const opts = baseOptions(suffix, yMax);
      opts.scales.x.title = { display: true, text: "Injected packet loss", color: css("--text-2"), font: { size: 11 } };
      opts.plugins.tooltip.callbacks.title = (items) => `${items[0].label} injected loss`;
      opts.plugins.tooltip.callbacks.label = (ctx) => ` ${ctx.dataset.label}: ${fmt(ctx.parsed.y, digits)}${suffix}`;
      const datasets = MODE_ORDER.map((m) => line(modeName(m), MODE_COLOR[m], {
        data: levels.map((l) => { const r = rowFor(summary, l, m); return r ? r[key] : null; }),
        pointRadius: 4, pointHoverRadius: 6, tension: 0,
        pointBorderColor: css("--surface"), pointBorderWidth: 2,
      }));
      resultCharts.push(new Chart($(id), {
        type: "line", data: { labels: levels.map((l) => `${l}%`), datasets }, options: opts,
      }));
    };
    make("#res-psnr", "psnr", " dB", 40, 1);
    make("#res-recovery", "recovery_pct", "%", 100, 0);
    make("#res-overhead", "overhead_pct", "%", 60, 0);
  }

  // Table: loss | recovered ×3 | PSNR ×3 | SSIM ×3 | overhead ×3
  const groups = [
    { key: "recovery_pct", label: "Frames recovered (%)", digits: 1, best: "max" },
    { key: "psnr", label: "PSNR (dB)", digits: 1, best: "max" },
    { key: "ssim", label: "SSIM", digits: 3, best: "max" },
    { key: "overhead_pct", label: "FEC overhead (%)", digits: 0, best: null },
  ];
  let html = "<thead><tr><th></th>" +
    groups.map((g) => `<th class="group sep" colspan="3">${g.label}</th>`).join("") +
    "</tr><tr><th>Injected loss</th>" +
    groups.map(() => MODE_ORDER.map((m, i) => `<th class="${i === 0 ? "sep" : ""}">${modeName(m)}</th>`).join("")).join("") +
    "</tr></thead><tbody>";
  for (const l of levels) {
    html += `<tr><td>${l}%</td>`;
    for (const g of groups) {
      const vals = MODE_ORDER.map((m) => { const r = rowFor(summary, l, m); return r ? r[g.key] : null; });
      const best = g.best ? Math.max(...vals.filter(isNum)) : null;
      vals.forEach((v, i) => {
        const cls = [i === 0 ? "sep" : "", g.best && isNum(v) && v.toFixed(g.digits) === best.toFixed(g.digits) ? "best" : ""].join(" ");
        html += `<td class="${cls}">${fmt(v, g.digits)}</td>`;
      });
    }
    html += "</tr>";
  }
  $("#results-table").innerHTML = html + "</tbody>";
  const files = summary.files || {};
  const hasTrace = (files.graphs || []).includes("adaptation_trace.png");
  $("#trace-card").hidden = !hasTrace;
  if (hasTrace) $("#trace-img").src = `/results/adaptation_trace.png?${Date.now()}`;
  $("#results-files").innerHTML = "Raw data: " +
    [files.csv, ...(files.graphs || [])].filter(Boolean)
      .map((f) => `<a href="/results/${f}" target="_blank" rel="noopener">${f}</a>`).join(" · ");
}

/* ------------------------------------------------------------ tabs & theme */

function showView(name) {
  for (const b of document.querySelectorAll(".segmented button")) {
    b.setAttribute("aria-selected", String(b.dataset.view === name));
  }
  $("#view-live").hidden = name !== "live";
  $("#view-results").hidden = name !== "results";
  if (name === "results") loadResults();
  history.replaceState(null, "", name === "results" ? "#results" : "#");
}

function restyleCharts() {
  if (!window.Chart) return;
  if (lossChart) { lossChart.destroy(); psnrChart.destroy(); buildLiveCharts(); }
  if (lastSnapshot) renderCharts(lastSnapshot);
  if (resultsLoaded) loadResults();
}

document.addEventListener("DOMContentLoaded", () => {
  buildMetricCards();
  buildLiveCharts();
  setupFeedToggle();
  for (const b of document.querySelectorAll(".segmented button")) {
    b.addEventListener("click", () => showView(b.dataset.view));
  }
  showView(location.hash === "#results" ? "results" : "live");
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", restyleCharts);
  connect();
});

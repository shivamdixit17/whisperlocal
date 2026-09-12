/* WhisperLocal dashboard — app.js
 *
 * Vanilla ES2020, no build step. Sections:
 *   1. DOM + formatting helpers
 *   2. API client (cookie auth; 401 -> auth wall, 503 -> in-place notice)
 *   3. Theme tokens + Chart.js defaults (re-read on colour-scheme change)
 *   4. Router (#dashboard / #meetings / #settings) + status pill polling
 *   5. Dashboard view
 *   6. Meetings view
 *   7. Settings view
 *   8. Boot
 */
'use strict';

/* ─── 1. Helpers ─────────────────────────────────────────────────────────── */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** el('div', {class: 'x', onclick: fn, dataset: {...}}, [children | text]) */
function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k === 'html') node.innerHTML = v; // only ever used with our own markup
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else if (k === 'style' && typeof v === 'object') {
      for (const [prop, val] of Object.entries(v)) { if (prop.startsWith('--')) node.style.setProperty(prop, val); else node.style[prop] = val; }
    }
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (v === true) node.setAttribute(k, '');
    else node.setAttribute(k, String(v));
  }
  for (const c of [].concat(children)) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}
function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); return node; }

const fmtInt = (n) => (n === null || n === undefined || Number.isNaN(n)) ? '–' : Math.round(n).toLocaleString();
const fmt1 = (n) => (n === null || n === undefined || Number.isNaN(n)) ? '–' : Number(n).toLocaleString(undefined, { maximumFractionDigits: 1 });
function compact(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return '–';
  const a = Math.abs(n);
  if (a >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
  if (a >= 1e4) return (n / 1e3).toFixed(1).replace(/\.0$/, '') + 'K';
  return Math.round(n).toLocaleString();
}
const pct = (n) => (n === null || n === undefined) ? '–' : `${Number(n).toFixed(n >= 10 ? 0 : 1)}%`;
function mmss(s) {
  s = Math.max(0, Math.round(s || 0));
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
}
function hhmmss(s) {
  s = Math.max(0, Math.floor(s || 0));
  return `${String(Math.floor(s / 3600)).padStart(2, '0')}:${mmss(s % 3600)}`;
}
function durationText(s) {
  if (s === null || s === undefined) return '–';
  if (s < 60) return `${Math.round(s)} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  return `${(s / 3600).toFixed(1)} h`;
}
function ms(v) { return v === null || v === undefined ? '–' : v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v)} ms`; }
/** '2026-09-12' -> local Date (no UTC shift) */
function localDate(ymd) { const [y, m, d] = ymd.split('-').map(Number); return new Date(y, m - 1, d); }
const fmtDay = (ymd) => localDate(ymd).toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
function fmtDateTime(iso) {
  if (!iso) return '–';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}
const debounce = (fn, wait = 250) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), wait); }; };
const sleep = (msec) => new Promise((r) => setTimeout(r, msec));

/* ─── Toasts ─────────────────────────────────────────────────────────────── */
function toast(title, { kind = 'info', lines = [], actions = [], timeout = 6000 } = {}) {
  const node = el('div', { class: `toast ${kind}`, role: 'status' }, [
    el('div', { text: title }),
    lines.length ? el('ul', {}, lines.map((l) => el('li', { text: l }))) : null,
  ]);
  if (actions.length) {
    node.append(el('div', { class: 'actions' }, actions.map((a) =>
      el('button', { type: 'button', class: `btn small ${a.primary ? 'primary' : ''}`, onclick: () => { a.onClick(); node.remove(); } }, a.label))));
  }
  const close = el('button', { type: 'button', class: 'btn small ghost', 'aria-label': 'Dismiss', style: { float: 'right', marginTop: '-4px' }, onclick: () => node.remove() }, '×');
  node.prepend(close);
  $('#toasts').append(node);
  if (timeout && !actions.length) setTimeout(() => node.remove(), timeout);
  return node;
}

/* ─── 2. API client ──────────────────────────────────────────────────────── */

/** fetch wrapper; resolves JSON, throws {status, error}. */
async function api(method, path, body) {
  const opts = { method, credentials: 'same-origin', headers: { Accept: 'application/json' } };
  if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  let res;
  try { res = await fetch(path, opts); }
  catch (e) { throw { status: 0, error: 'Cannot reach the WhisperLocal server.' }; }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
  if (res.status === 401) { $('#wall').hidden = false; throw { status: 401, error: 'unauthorized' }; }
  if (!res.ok) throw { status: res.status, error: (data && data.error) || `${res.status} ${res.statusText}` };
  return data;
}
const errText = (e) => (e && e.error) || (e && e.message) || String(e);

/* ─── 3. Theme tokens & Chart.js defaults ────────────────────────────────── */

const TOKEN_NAMES = ['surface', 'surface-2', 'ink', 'ink-2', 'muted', 'grid', 'axis', 'border', 'accent',
  's1', 's2', 's3', 'seq-300', 'seq-600', 'good', 'warning', 'serious', 'critical'];
let T = {};
function readTheme() {
  const cs = getComputedStyle(document.documentElement);
  T = {};
  for (const n of TOKEN_NAMES) T[n.replace('-', '_')] = cs.getPropertyValue('--' + n).trim();
  return T;
}
/** '#rrggbb' + alpha -> rgba() */
function alpha(hex, a) {
  const m = /^#([0-9a-f]{6})$/i.exec(hex);
  if (!m) return hex;
  const v = parseInt(m[1], 16);
  return `rgba(${(v >> 16) & 255}, ${(v >> 8) & 255}, ${v & 255}, ${a})`;
}

function applyChartDefaults() {
  if (!window.Chart) return;
  readTheme();
  const d = Chart.defaults;
  d.font.family = getComputedStyle(document.body).fontFamily;
  d.font.size = 12;
  d.color = T.muted;
  d.borderColor = T.grid;
  d.animation = false;
  d.responsive = true;
  d.maintainAspectRatio = false;
  d.plugins.legend.display = false;
  d.plugins.legend.labels.boxWidth = 10;
  d.plugins.legend.labels.boxHeight = 10;
  d.plugins.legend.labels.color = T.ink_2;
  Object.assign(d.plugins.tooltip, {
    backgroundColor: T.surface, titleColor: T.ink, bodyColor: T.ink_2, borderColor: T.axis, borderWidth: 1,
    padding: 8, boxPadding: 4, boxWidth: 8, boxHeight: 8, cornerRadius: 6, titleFont: { weight: '600' },
  });
  d.elements.bar.borderRadius = 4;         // rounded data end, square baseline (borderSkipped: 'start')
  d.elements.bar.borderSkipped = 'start';
  d.elements.line.borderWidth = 2;
  d.elements.line.borderJoinStyle = 'round';
  d.elements.line.borderCapStyle = 'round';
  d.elements.point.radius = 0;
  d.elements.point.hoverRadius = 5;
  d.elements.point.hitRadius = 12;
  d.elements.point.borderWidth = 2;
  d.elements.point.borderColor = T.surface; // 2px surface ring
  d.datasets.bar.maxBarThickness = 24;
  d.scale.grid.color = T.grid;
  d.scale.grid.drawTicks = false;
  d.scale.border.color = T.axis;
  d.scale.ticks.color = T.muted;
}

/** Crosshair: a hairline at the active x on line charts (interaction mode 'index'). */
const crosshairPlugin = {
  id: 'wlCrosshair',
  afterDraw(chart) {
    if (!chart.options.plugins.wlCrosshair || !chart.tooltip || !chart.tooltip.getActiveElements().length) return;
    const x = chart.tooltip.getActiveElements()[0].element.x;
    const { top, bottom } = chart.chartArea;
    const ctx = chart.ctx;
    ctx.save();
    ctx.strokeStyle = T.axis; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom); ctx.stroke();
    ctx.restore();
  },
};

/** Every mounted chart: canvas -> config factory, so a theme flip can rebuild them. */
const charts = new Map();
function mountChart(canvas, makeConfig, summary) {
  if (!canvas || !window.Chart) return null;
  const prev = charts.get(canvas);
  if (prev && prev.chart) prev.chart.destroy();
  const cfg = makeConfig();
  cfg.plugins = [crosshairPlugin].concat(cfg.plugins || []);
  const chart = new Chart(canvas, cfg);
  canvas.setAttribute('role', 'img');
  if (summary) canvas.setAttribute('aria-label', summary);
  charts.set(canvas, { chart, makeConfig, summary });
  return chart;
}
function rebuildCharts() {
  applyChartDefaults();
  for (const [canvas, entry] of Array.from(charts.entries())) {
    if (!canvas.isConnected) { entry.chart.destroy(); charts.delete(canvas); continue; }
    mountChart(canvas, entry.makeConfig, entry.summary);
  }
}
const darkMedia = window.matchMedia('(prefers-color-scheme: dark)');
darkMedia.addEventListener('change', rebuildCharts);

/** Common scale options for a category x / linear y chart. */
function scales({ yTitle, yFmt, xFmt, xMax = 8, stacked = false, horizontal = false } = {}) {
  // Only set a tick callback when we have one: `callback: undefined` would override Chart.js's default label lookup.
  const catTicks = { autoSkip: true, maxTicksLimit: xMax, maxRotation: 0 };
  if (xFmt) catTicks.callback = xFmt;
  const cat = { grid: { display: false }, ticks: catTicks, stacked };
  const lin = { beginAtZero: true, grid: { color: T.grid }, border: { display: false }, ticks: { maxTicksLimit: 5, callback: yFmt || ((v) => compact(v)) }, stacked };
  if (yTitle) lin.title = { display: true, text: yTitle, color: T.muted };
  if (horizontal) return { x: lin, y: { ...cat, ticks: { ...catTicks, autoSkip: false } } };
  return { x: cat, y: lin };
}
function noDataOverlay(box, text = 'No data yet') {
  clear(box).append(el('div', { class: 'empty small', text }));
}

/* ─── 4. Router & status ─────────────────────────────────────────────────── */

const TABS = ['dashboard', 'meetings', 'settings'];
let currentTab = null;
function route() {
  const hash = (location.hash || '#dashboard').slice(1);
  let [tab, sub] = hash.split('/');             // #meetings/<id> deep-links a meeting
  if (!TABS.includes(tab)) { tab = 'dashboard'; sub = ''; }
  for (const a of $$('.tab')) { if (a.dataset.tab === tab) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current'); }
  for (const v of $$('.view')) v.classList.toggle('active', v.id === `view-${tab}`);
  $('#savebar').hidden = !(tab === 'settings' && Object.keys(settingsState.dirty).length);
  if (tab === currentTab) { if (tab === 'meetings' && sub) meetings.open(sub); return; }
  currentTab = tab;
  if (tab === 'dashboard') dashboard.load();
  else if (tab === 'meetings') meetings.load(sub);
  else if (tab === 'settings') settings.load();
}
window.addEventListener('hashchange', route);

const STATE_LABEL = { idle: 'Idle', waiting: 'Waiting', recording: 'Recording', transcribing: 'Transcribing' };
let lastStatus = null;
async function pollStatus() {
  const pill = $('#status-pill'), text = $('#status-text');
  try {
    const s = await api('GET', '/api/status');
    lastStatus = s;
    if (!s.running) { pill.dataset.state = 'off'; text.textContent = 'App not running'; }
    else if (s.meeting && s.meeting.recording) {
      pill.dataset.state = 'meeting';
      text.textContent = `● Recording meeting ${mmss(s.meeting.elapsed_s)}`;
    } else if (s.enabled === false) { pill.dataset.state = 'disabled'; text.textContent = `Disabled · ${s.trigger_label || ''}`.replace(/ · $/, ''); }
    else { pill.dataset.state = s.state || 'idle'; text.textContent = `${STATE_LABEL[s.state] || s.state || 'Idle'}${s.trigger_label ? ' · ' + s.trigger_label : ''}`; }
  } catch (e) {
    if (e.status === 401) return;
    lastStatus = e.status === 503 ? { running: false } : null;
    pill.dataset.state = 'off';
    text.textContent = e.status === 503 || e.status === 0 ? 'App not running' : `Status unavailable (${e.status})`;
  }
  meetings.syncRecordButton();
}

/* ─── 5. Dashboard ───────────────────────────────────────────────────────── */

const STATUS_LABEL = { ok: 'OK', hallucination: 'Hallucination', too_short: 'Too short', empty: 'Empty', error: 'Error', unknown: 'Unknown' };
const STATUS_COLOR = () => ({ ok: T.good, hallucination: T.critical, too_short: T.warning, empty: T.serious, error: T.critical, unknown: T.muted });
const statusBadge = (s) => el('span', { class: `badge ${s === 'ok' ? 'ok' : s === 'hallucination' || s === 'error' ? 'bad' : 'warn'}`, text: STATUS_LABEL[s] || s || '–' });

const dashboard = {
  days: 30, typingWpm: 40, data: null, loading: false,
  history: { q: '', status: '', app: '', page: 0, limit: 25, total: 0 },

  async load() {
    const body = $('#dash-body'), notice = $('#dash-notice');
    if (!this.data) clear(body).append(el('div', { class: 'empty', text: 'Loading…' }));
    body.classList.add('loading');
    this.loading = true;
    try {
      const days = this.days === 'all' ? '' : this.days;
      const data = await api('GET', `/api/stats?days=${days}&typing_wpm=${this.typingWpm}`);
      this.data = data;
      notice.hidden = true;
      this.render();
      this.loadHistory(true);
    } catch (e) {
      if (e.status === 401) return;
      notice.hidden = false; notice.className = 'notice error';
      notice.textContent = e.status === 503 ? errText(e) : `Could not load statistics: ${errText(e)}`;
      if (!this.data) clear(body);
    } finally { body.classList.remove('loading'); this.loading = false; }
  },

  render() {
    const d = this.data, body = clear($('#dash-body'));
    if (!d || !d.totals || !d.totals.dictations) {
      body.append(el('div', { class: 'empty' }, [
        el('div', { text: 'No dictations in this range yet.' }),
        el('div', { class: 'small', text: d && d.history_enabled === false ? 'History is turned off in Settings, so nothing is recorded.' : 'Hold your trigger key and talk — the first entry appears here right away.' }),
      ]));
      return;
    }
    const t = d.totals, st = d.streaks || {}, w = d.wpm || {};
    const saved = t.minutes_saved;
    // Stat tiles
    body.append(el('div', { class: 'tiles' }, [
      tile('Dictations', fmtInt(t.dictations), `${fmtInt(t.ok)} ok · ${pct(t.dictations ? 100 * t.ok / t.dictations : 0)}`),
      tile('Words', compact(t.words), `${fmtInt(t.avg_words)} per dictation`),
      tile('Minutes speaking', fmt1(t.minutes_speaking), `${fmt1(t.avg_seconds)} s per dictation`),
      tile('Time saved', (saved < 0 ? '−' : '~') + fmt1(Math.abs(saved || 0)) + ' min', `vs typing at ${t.typing_wpm || this.typingWpm} wpm`, saved < 0),
      tile('Current streak', `${fmtInt(st.current_days)} d`, `longest ${fmtInt(st.longest_days)} d · ${fmtInt(st.active_days)} active days`),
      tile('Average wpm', fmt1(t.avg_wpm), w.p50 ? `median ${fmt1(w.p50)} · p90 ${fmt1(w.p90)}` : ''),
    ]));

    const grid = el('div', { class: 'grid' });
    body.append(grid);
    grid.append(this.cardWordsPerDay(d), this.cardWordsPerWeek(d), this.cardHeatmap(d), this.cardOutcomes(d),
      this.cardApps(d), this.cardWpm(d), this.cardLatency(d), this.cardLengths(d), this.cardTerms(d));
    body.append(this.cardHistory(d));
  },

  /* Words per day — single-series line + 10% area wash */
  cardWordsPerDay(d) {
    const rows = d.daily || [];
    const { card, box, canvas } = chartCard('Words per day', `${fmtInt(rows.reduce((a, r) => a + r.words, 0))} words over ${rows.length} days`);
    if (!rows.length) { noDataOverlay(box); return card; }
    const peak = rows.reduce((m, r) => r.words > m.words ? r : m, rows[0]);
    mountChart(canvas, () => ({
      type: 'line',
      data: { labels: rows.map((r) => r.date), datasets: [{ label: 'Words', data: rows.map((r) => r.words), borderColor: T.s1, backgroundColor: alpha(T.s1, 0.1), fill: true, tension: 0.25, pointBackgroundColor: T.s1 }] },
      options: {
        interaction: { mode: 'index', intersect: false },
        plugins: { wlCrosshair: true, tooltip: { callbacks: { title: (it) => fmtDay(it[0].label), afterBody: (it) => { const r = rows[it[0].dataIndex]; return [`${r.dictations} dictations · ${fmt1(r.minutes)} min`]; } } } },
        scales: scales({ xFmt: function (v) { return fmtDay(this.getLabelForValue(v)); }, xMax: rows.length > 40 ? 6 : 8 }),
      },
    }), `Words per day. Peak ${fmtInt(peak.words)} words on ${fmtDay(peak.date)}.`);
    return card;
  },

  /* Words per week — bars, delta % in tooltip, "vs last week" under title */
  cardWordsPerWeek(d) {
    const rows = d.weekly || [];
    const last = rows[rows.length - 1];
    const delta = last && last.delta_pct !== null && last.delta_pct !== undefined ? last.delta_pct : null;
    const sub = delta === null ? 'not enough weeks to compare' : `${delta >= 0 ? '+' : ''}${fmt1(delta)}% words vs last week`;
    const { card, box, canvas } = chartCard('Words per week', sub);
    if (!rows.length) { noDataOverlay(box); return card; }
    mountChart(canvas, () => ({
      type: 'bar',
      data: { labels: rows.map((r) => r.week_start), datasets: [{ label: 'Words', data: rows.map((r) => r.words), backgroundColor: T.s1 }] },
      options: {
        plugins: { tooltip: { callbacks: {
          title: (it) => `Week of ${fmtDay(it[0].label)}`,
          afterBody: (it) => { const r = rows[it[0].dataIndex]; const dl = r.delta_pct === null || r.delta_pct === undefined ? 'no previous week' : `${r.delta_pct >= 0 ? '+' : ''}${fmt1(r.delta_pct)}% vs previous week`; return [`${r.dictations} dictations · ${fmt1(r.minutes)} min`, dl]; },
        } } },
        scales: scales({ xFmt: function (v) { return fmtDay(this.getLabelForValue(v)); }, xMax: 12 }),
      },
    }), `Words per week for the last ${rows.length} weeks; latest week ${fmtInt(last.words)} words.`);
    return card;
  },

  /* Hour x weekday heatmap — CSS grid, sequential blue by alpha */
  cardHeatmap(d) {
    const h = d.heatmap || { rows: [], counts: [], max: 0 };
    const card = el('div', { class: 'card' }, [el('div', { class: 'card-head' }, [el('h3', { text: 'When you dictate' })]),
      el('p', { class: 'card-sub', text: h.max ? `Busiest cell: ${h.max} dictations` : 'No data yet' })]);
    const grid = el('div', { class: 'heatmap', role: 'table', 'aria-label': 'Dictations by weekday and hour' });
    grid.append(el('div'));
    for (let c = 0; c < 24; c++) grid.append(el('div', { class: 'cl', text: c % 3 === 0 ? String(c) : '' }));
    (h.rows || []).forEach((name, r) => {
      grid.append(el('div', { class: 'rl', text: name }));
      for (let c = 0; c < 24; c++) {
        const n = (h.counts[r] || [])[c] || 0;
        const a = h.max ? Math.max(n ? 0.12 : 0, n / h.max) : 0;
        grid.append(el('div', { class: 'cell', style: { '--a': a.toFixed(3) }, title: `${name} ${String(c).padStart(2, '0')}:00 — ${n} dictation${n === 1 ? '' : 's'}`, tabindex: n ? 0 : null, 'aria-label': `${name} ${c}:00, ${n} dictations` }));
      }
    });
    card.append(grid);
    return card;
  },

  /* Outcomes — doughnut (status colours) + rates trend line */
  cardOutcomes(d) {
    const o = d.outcomes || { counts: {}, total: 0, rates: {}, trend: [] };
    const keys = Object.keys(o.counts || {});
    const card = el('div', { class: 'card' }, [el('div', { class: 'card-head' }, [el('h3', { text: 'Outcomes' }), el('span', { class: 'small muted', text: `${fmtInt(o.total)} total` })])]);
    if (!keys.length) { card.append(el('div', { class: 'empty small', text: 'No data yet' })); return card; }
    const wrap = el('div', { class: 'two-up' });
    const dBox = el('div', { class: 'chart short' }), dCanvas = el('canvas');
    const tBox = el('div', { class: 'chart short' }), tCanvas = el('canvas');
    dBox.append(dCanvas); tBox.append(tCanvas);
    const legend = el('div', { class: 'legend' });
    wrap.append(el('div', {}, [dBox, legend]), el('div', {}, [el('div', { class: 'small muted', text: 'Weekly rates' }), tBox]));
    card.append(wrap);
    mountChart(dCanvas, () => ({
      type: 'doughnut',
      data: { labels: keys.map((k) => STATUS_LABEL[k] || k), datasets: [{ data: keys.map((k) => o.counts[k]), backgroundColor: keys.map((k) => STATUS_COLOR()[k] || T.muted), borderColor: T.surface, borderWidth: 2, hoverOffset: 4 }] },
      options: { cutout: '65%', plugins: { tooltip: { callbacks: { label: (it) => ` ${fmtInt(it.parsed)} · ${pct(o.rates[keys[it.dataIndex]])}` } } } },
    }), `Outcomes: ${keys.map((k) => `${STATUS_LABEL[k] || k} ${pct(o.rates[k])}`).join(', ')}.`);
    for (const k of keys) legend.append(el('span', {}, [el('span', { class: 'swatch', style: { background: STATUS_COLOR()[k] || T.muted } }), `${STATUS_LABEL[k] || k} ${fmtInt(o.counts[k])} (${pct(o.rates[k])})`]));
    const tr = o.trend || [];
    if (tr.length < 2) { noDataOverlay(tBox, 'Not enough weeks'); }
    else {
      mountChart(tCanvas, () => ({
        type: 'line',
        data: { labels: tr.map((r) => r.week_start), datasets: [
          { label: 'OK', data: tr.map((r) => r.ok_pct), borderColor: T.good, pointBackgroundColor: T.good },
          { label: 'Hallucination', data: tr.map((r) => r.hallucination_pct), borderColor: T.critical, pointBackgroundColor: T.critical },
          { label: 'Too short', data: tr.map((r) => r.too_short_pct), borderColor: T.warning, pointBackgroundColor: T.warning },
        ] },
        options: {
          interaction: { mode: 'index', intersect: false },
          plugins: { wlCrosshair: true, legend: { display: true, position: 'bottom', labels: { boxHeight: 2, boxWidth: 14 } }, tooltip: { callbacks: { title: (it) => `Week of ${fmtDay(it[0].label)}`, label: (it) => ` ${it.dataset.label}: ${pct(it.parsed.y)}` } } },
          scales: { ...scales({ xFmt: function (v) { return fmtDay(this.getLabelForValue(v)); }, xMax: 6, yFmt: (v) => v + '%' }), y: { ...scales({ yFmt: (v) => v + '%' }).y, max: 100 } },
        },
      }), 'Weekly outcome rates for OK, hallucination and too-short dictations.');
    }
    return card;
  },

  /* Per-app horizontal bars + table */
  cardApps(d) {
    const apps = d.apps || [];
    const { card, box, canvas } = chartCard('By app', `${apps.length} apps`, 'tall');
    if (!apps.length) { noDataOverlay(box); return card; }
    box.style.height = `${Math.max(120, 30 * apps.length + 40)}px`;
    mountChart(canvas, () => ({
      type: 'bar',
      data: { labels: apps.map((a) => a.app), datasets: [{ label: 'Dictations', data: apps.map((a) => a.dictations), backgroundColor: T.s1 }] },
      options: { indexAxis: 'y', plugins: { tooltip: { callbacks: { afterBody: (it) => { const a = apps[it[0].dataIndex]; return [`${compact(a.words)} words · ${fmt1(a.minutes)} min`]; } } } }, scales: scales({ horizontal: true }) },
    }), `Dictations by app: ${apps.slice(0, 3).map((a) => `${a.app} ${a.dictations}`).join(', ')}.`);
    const tbl = el('table', {}, [
      el('thead', {}, el('tr', {}, [th('App'), th('Dictations', 'num'), th('Words', 'num'), th('Hallucination', 'num'), th('Short tap', 'num')])),
      el('tbody', {}, apps.map((a) => el('tr', {}, [td(a.app), td(fmtInt(a.dictations), 'num'), td(compact(a.words), 'num'), td(pct(a.hallucination_pct), 'num'), td(pct(a.too_short_pct), 'num')]))),
    ]);
    card.append(el('div', { class: 'table-wrap', style: { marginTop: '0.6rem' } }, tbl));
    return card;
  },

  /* WPM histogram — capped at 300, tail folded into "300+" */
  cardWpm(d) {
    const w = d.wpm || { bins: [] };
    const { card, box, canvas } = chartCard('Speaking speed', w.n ? `median ${fmt1(w.p50)} wpm · p90 ${fmt1(w.p90)} · ${fmtInt(w.n)} dictations` : 'No data yet');
    if (!w.bins || !w.bins.length) { noDataOverlay(box); return card; }
    const CAP = 300;
    const bins = w.bins.filter((b) => b.from < CAP).map((b) => ({ label: `${b.from}–${b.to}`, count: b.count }));
    const tail = w.bins.filter((b) => b.from >= CAP).reduce((a, b) => a + b.count, 0);
    if (tail) bins.push({ label: `${CAP}+`, count: tail });
    mountChart(canvas, () => ({
      type: 'bar',
      data: { labels: bins.map((b) => b.label), datasets: [{ label: 'Dictations', data: bins.map((b) => b.count), backgroundColor: T.s1, barPercentage: 1, categoryPercentage: 0.9 }] },
      options: { plugins: { tooltip: { callbacks: { title: (it) => `${it[0].label} wpm` } } }, scales: scales({ xMax: 10 }) },
    }), `Distribution of words per minute; median ${fmt1(w.p50)}.`);
    return card;
  },

  /* Latency by model — grouped p50/p95 (two shades of the sequential hue) */
  cardLatency(d) {
    const rows = d.latency || [];
    const worst = rows.reduce((m, r) => (r.max_ms > (m ? m.max_ms : -1) ? r : m), null);
    const bk = d.backends || {};
    const bkText = ['local', 'api'].filter((k) => bk[k] && bk[k].dictations).map((k) => `${k} ${fmtInt(bk[k].dictations)}`).join(' · ');
    const { card, box, canvas } = chartCard('Transcription latency', worst ? `slowest single run ${ms(worst.max_ms)} (${worst.short})${bkText ? ' · ' + bkText : ''}` : 'No data yet');
    if (!rows.length) { noDataOverlay(box); return card; }
    mountChart(canvas, () => ({
      type: 'bar',
      data: { labels: rows.map((r) => r.short), datasets: [
        { label: 'p50', data: rows.map((r) => r.p50_ms), backgroundColor: T.seq_300 },
        { label: 'p95', data: rows.map((r) => r.p95_ms), backgroundColor: T.seq_600 },
      ] },
      options: {
        plugins: { legend: { display: true, position: 'bottom' }, tooltip: { callbacks: { label: (it) => ` ${it.dataset.label}: ${ms(it.parsed.y)}`, afterBody: (it) => { const r = rows[it[0].dataIndex]; return [`n=${r.n} · mean ${ms(r.mean_ms)} · max ${ms(r.max_ms)}`]; } } } },
        scales: scales({ yFmt: (v) => ms(v), xMax: 8 }),
      },
    }), `Latency by model: ${rows.map((r) => `${r.short} p50 ${ms(r.p50_ms)}, p95 ${ms(r.p95_ms)}`).join('; ')}.`);
    return card;
  },

  /* Dictation length buckets — ordinal (one hue) */
  cardLengths(d) {
    const b = (d.lengths && d.lengths.buckets) || [];
    const { card, box, canvas } = chartCard('Dictation length', 'seconds of audio per dictation');
    if (!b.length || !b.some((x) => x.count)) { noDataOverlay(box); return card; }
    mountChart(canvas, () => ({
      type: 'bar',
      data: { labels: b.map((x) => x.label), datasets: [{ label: 'Dictations', data: b.map((x) => x.count), backgroundColor: T.s1 }] },
      options: { scales: scales({ xMax: 12 }) },
    }), `Dictation length buckets: ${b.map((x) => `${x.label} ${x.count}`).join(', ')}.`);
    return card;
  },

  /* Top terms — sized list */
  cardTerms(d) {
    const t = d.terms || {};
    const card = el('div', { class: 'card span-2' }, [el('div', { class: 'card-head' }, [el('h3', { text: 'Top terms' })])]);
    if (!t.has_text) {
      card.append(el('p', { class: 'small muted', text: 'Words are not stored (History → “Store the words” is off), so there is nothing to count.' }));
      return card;
    }
    const cloud = (items, title) => {
      if (!items || !items.length) return null;
      const max = items[0].count || 1, min = items[items.length - 1].count || 1;
      const list = el('div', { class: 'terms', role: 'list' });
      for (const it of items) {
        const size = 0.8 + (max === min ? 0.3 : 0.9 * (it.count - min) / (max - min));
        list.append(el('span', { role: 'listitem', style: { fontSize: `${size.toFixed(2)}rem` }, title: `${it.count}×` }, [it.term, el('b', { text: it.count })]));
      }
      return el('div', { style: { marginBottom: '0.6rem' } }, [el('div', { class: 'small muted', text: title }), list]);
    };
    card.append(cloud(t.unigrams, 'Words') || el('p', { class: 'small muted', text: 'No terms yet' }), cloud(t.bigrams, 'Phrases'));
    return card;
  },

  /* Recent dictations table with filters + pager */
  cardHistory(d) {
    const h = this.history;
    const card = el('div', { class: 'card', style: { marginTop: '1rem' } });
    const statusSel = el('select', { id: 'hist-status', 'aria-label': 'Status filter', onchange: () => { h.status = statusSel.value; h.page = 0; this.loadHistory(); } },
      [el('option', { value: '', text: 'All statuses' })].concat(Object.keys(STATUS_LABEL).filter((k) => k !== 'unknown').map((k) => el('option', { value: k, text: STATUS_LABEL[k], selected: h.status === k || null }))));
    const appSel = el('select', { id: 'hist-app', 'aria-label': 'App filter', onchange: () => { h.app = appSel.value; h.page = 0; this.loadHistory(); } },
      [el('option', { value: '', text: 'All apps' })].concat((d.apps || []).map((a) => el('option', { value: a.app, text: a.app, selected: h.app === a.app || null }))));
    const search = el('input', { type: 'search', id: 'hist-q', placeholder: 'Search text…', value: h.q, 'aria-label': 'Search dictations', oninput: debounce(() => { h.q = search.value.trim(); h.page = 0; this.loadHistory(); }, 300) });
    card.append(el('div', { class: 'card-head' }, [el('h3', { text: 'Recent dictations' }), search, statusSel, appSel]));
    card.append(el('div', { class: 'table-wrap', id: 'hist-table' }), el('div', { class: 'pager', id: 'hist-pager' }));
    return card;
  },

  async loadHistory(reset) {
    const h = this.history;
    if (reset) { h.page = 0; }
    const wrap = $('#hist-table'), pager = $('#hist-pager');
    if (!wrap) return;
    wrap.classList.add('loading');
    const days = this.days === 'all' ? '' : this.days;
    const qs = new URLSearchParams({ limit: h.limit, offset: h.page * h.limit, q: h.q, status: h.status, app: h.app, days });
    try {
      const r = await api('GET', `/api/history?${qs}`);
      h.total = r.total || 0;
      const items = r.items || [];
      const showText = !!r.has_text;
      clear(wrap);
      if (!items.length) { wrap.append(el('div', { class: 'empty small', text: 'No dictations match.' })); }
      else {
        wrap.append(el('table', {}, [
          el('thead', {}, el('tr', {}, [th('When'), th('Status'), showText ? th('Text') : null, th('Words', 'num'), th('Audio', 'num'), th('Latency', 'num'), th('WPM', 'num'), th('App'), th('Model')])),
          el('tbody', {}, items.map((it) => el('tr', {}, [
            td(fmtDateTime(it.timestamp), 'num'), el('td', {}, statusBadge(it.status)),
            showText ? el('td', {}, el('span', { class: 'text', title: it.text || '', text: it.text || '' })) : null,
            td(fmtInt(it.words), 'num'), td(it.audio_seconds !== null && it.audio_seconds !== undefined ? `${fmt1(it.audio_seconds)} s` : '–', 'num'),
            td(ms(it.transcribe_ms), 'num'), td(it.wpm ? fmtInt(it.wpm) : '–', 'num'), td(it.app || '–'), td(it.model || (it.backend === 'api' ? 'api' : '–')),
          ]))),
        ]));
      }
      const pages = Math.max(1, Math.ceil(h.total / h.limit));
      clear(pager).append(
        el('span', { text: `${fmtInt(h.total)} dictations · page ${h.page + 1} of ${pages}` }), el('span', { class: 'spacer' }),
        el('button', { type: 'button', class: 'btn small', disabled: h.page === 0 || null, onclick: () => { h.page--; this.loadHistory(); } }, '‹ Prev'),
        el('button', { type: 'button', class: 'btn small', disabled: h.page + 1 >= pages || null, onclick: () => { h.page++; this.loadHistory(); } }, 'Next ›'),
      );
    } catch (e) {
      if (e.status !== 401) clear(wrap).append(el('div', { class: 'notice error', text: errText(e) }));
    } finally { wrap.classList.remove('loading'); }
  },
};

/* Dashboard building blocks */
function tile(label, value, sub, negative) {
  return el('div', { class: 'card tile' }, [el('div', { class: 'label', text: label }), el('div', { class: `value ${negative ? 'neg' : ''}`, text: value }), el('div', { class: 'sub', text: sub || '' })]);
}
function chartCard(title, sub, size = '') {
  const box = el('div', { class: `chart ${size}` }), canvas = el('canvas');
  box.append(canvas);
  const card = el('div', { class: 'card' }, [el('div', { class: 'card-head' }, [el('h3', { text: title })]), sub ? el('p', { class: 'card-sub', text: sub }) : null, box]);
  return { card, box, canvas };
}
const th = (t, cls) => el('th', { class: cls || null, text: t });
const td = (t, cls) => el('td', { class: cls || null, text: t });

/* Filter row wiring */
$('#range-seg').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-days]');
  if (!b) return;
  for (const x of $$('#range-seg button')) x.setAttribute('aria-pressed', x === b ? 'true' : 'false');
  dashboard.days = b.dataset.days === 'all' ? 'all' : Number(b.dataset.days);
  dashboard.load();
});
$('#typing-wpm').addEventListener('change', () => {
  const v = Number($('#typing-wpm').value);
  dashboard.typingWpm = v > 0 ? v : 40;
  dashboard.load();
});

/* ─── 6. Meetings ────────────────────────────────────────────────────────── */

const meetings = {
  q: '', page: 0, limit: 25, total: 0, items: [], stats: null, selected: null, unavailable: false, busy: false,

  async load(openId) {
    const body = $('#meet-body'), notice = $('#meet-notice');
    if (!body.childElementCount) clear(body).append(el('div', { class: 'empty', text: 'Loading…' }));
    try {
      const [stats, list] = await Promise.all([api('GET', '/api/meetings/stats?days='), this.fetchList()]);
      this.stats = stats;
      this.unavailable = false;
      notice.hidden = true;
      this.render(list);
      if (openId) this.open(openId);
    } catch (e) {
      if (e.status === 401) return;
      this.unavailable = e.status === 503;
      notice.hidden = false; notice.className = `notice ${e.status === 503 ? '' : 'error'}`;
      notice.textContent = e.status === 503 ? errText(e) || 'Meeting recording is not available.' : `Could not load meetings: ${errText(e)}`;
      clear(body);
      this.syncRecordButton();
    }
  },

  fetchList() {
    const qs = new URLSearchParams({ limit: this.limit, offset: this.page * this.limit, q: this.q, days: '' });
    return api('GET', `/api/meetings?${qs}`);
  },

  render(list) {
    const body = clear($('#meet-body')), s = this.stats || {};
    const you = (s.talk_time && s.talk_time.You) || 0, others = (s.talk_time && s.talk_time.Others) || 0;
    const talk = el('div', { class: 'card tile' }, [
      el('div', { class: 'label', text: 'Talk time' }),
      el('div', { class: 'value', style: { fontSize: '1.1rem' }, text: `You ${pct(you)} · Others ${pct(others)}` }),
      el('div', { class: 'talk-bar', role: 'img', 'aria-label': `You ${pct(you)}, others ${pct(others)}` }, [
        el('span', { class: 'you', style: { width: `${you}%` } }), el('span', { class: 'others', style: { width: `${others}%` } })]),
      el('div', { class: 'legend' }, [
        el('span', {}, [el('span', { class: 'swatch', style: { background: T.s1 } }), 'You']),
        el('span', {}, [el('span', { class: 'swatch', style: { background: T.s2 } }), 'Others'])]),
    ]);
    body.append(el('div', { class: 'tiles tiles-4' }, [
      tile('Meetings', fmtInt(s.meetings), `${compact(s.words_total)} words transcribed`),
      tile('Hours recorded', fmt1(s.hours_total), ''),
      tile('Average length', `${fmt1(s.avg_minutes)} min`, s.longest && s.longest[0] ? `longest ${fmt1(s.longest[0].minutes)} min` : ''),
      talk,
    ]));
    const search = el('input', { type: 'search', placeholder: 'Search meetings…', value: this.q, 'aria-label': 'Search meetings', oninput: debounce(async () => { this.q = search.value.trim(); this.page = 0; this.refreshList(); }, 300) });
    const card = el('div', { class: 'card' }, [el('div', { class: 'card-head' }, [el('h3', { text: 'Recordings' }), search]), el('div', { class: 'table-wrap', id: 'meet-table' }), el('div', { class: 'pager', id: 'meet-pager' })]);
    body.append(card, el('div', { id: 'meet-detail' }));
    this.renderList(list);
    this.syncRecordButton();
  },

  async refreshList() {
    try { this.renderList(await this.fetchList()); } catch (e) { if (e.status !== 401) toast(errText(e), { kind: 'error' }); }
  },

  renderList(list) {
    const wrap = $('#meet-table'), pager = $('#meet-pager');
    if (!wrap) return;
    this.total = list.total || 0; this.items = list.items || [];
    clear(wrap);
    if (!this.items.length) wrap.append(el('div', { class: 'empty small', text: this.q ? 'No meetings match.' : 'No meetings recorded yet. Press Record when a call is on, or turn on meeting detection in Settings.' }));
    else {
      wrap.append(el('table', {}, [
        el('thead', {}, el('tr', {}, [th('Started'), th('Title'), th('Duration', 'num'), th('Words', 'num'), th('App'), th('Status')])),
        el('tbody', {}, this.items.map((m) => {
          const tr = el('tr', { class: `row-btn ${this.selected && this.selected.id === m.id ? 'selected' : ''}`, onclick: () => this.open(m.id) }, [
            td(fmtDateTime(m.started_at), 'num'),
            el('td', {}, el('button', { type: 'button', class: 'btn ghost small', style: { padding: 0, fontWeight: 500 }, onclick: (e) => { e.stopPropagation(); this.open(m.id); } }, m.title || 'Untitled meeting')),
            td(durationText(m.duration_s), 'num'), td(compact(m.words_total), 'num'), td(m.app || '–'), el('td', {}, meetingBadge(m.status)),
          ]);
          return tr;
        })),
      ]));
    }
    const pages = Math.max(1, Math.ceil(this.total / this.limit));
    clear(pager).append(
      el('span', { text: `${fmtInt(this.total)} meetings · page ${this.page + 1} of ${pages}` }), el('span', { class: 'spacer' }),
      el('button', { type: 'button', class: 'btn small', disabled: this.page === 0 || null, onclick: () => { this.page--; this.refreshList(); } }, '‹ Prev'),
      el('button', { type: 'button', class: 'btn small', disabled: this.page + 1 >= pages || null, onclick: () => { this.page++; this.refreshList(); } }, 'Next ›'),
    );
  },

  async open(id) {
    const box = $('#meet-detail');
    if (!box) return;
    clear(box).append(el('div', { class: 'card detail', text: 'Loading…' }));
    try {
      const m = await api('GET', `/api/meetings/${encodeURIComponent(id)}`);
      this.selected = m;
      if (location.hash !== `#meetings/${id}`) history.replaceState(null, '', `#meetings/${id}`);
      for (const tr of $$('#meet-table tr.row-btn')) tr.classList.remove('selected');
      this.renderDetail(m);
      box.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (e) { if (e.status !== 401) clear(box).append(el('div', { class: 'notice error', text: errText(e) })); }
  },

  renderDetail(m) {
    const box = clear($('#meet-detail'));
    const st = m.stats || {};
    const card = el('div', { class: 'card detail' });
    card.append(el('div', { class: 'card-head' }, [el('h3', { text: m.title || 'Untitled meeting' }), meetingBadge(m.status),
      el('button', { type: 'button', class: 'btn small ghost', 'aria-label': 'Close details', onclick: () => { this.selected = null; clear(box); history.replaceState(null, '', '#meetings'); } }, '×')]));
    if (m.status === 'recording' || m.status === 'transcribing') {
      card.append(el('p', { class: 'small' }, [el('span', { class: 'spinner' }), ' ', m.status === 'recording' ? 'Recording in progress — the transcript fills in as segments finish.' : 'Transcribing… this page refreshes when it is done.']));
      setTimeout(() => { if (this.selected && this.selected.id === m.id && currentTab === 'meetings') this.open(m.id); }, 5000);
    }
    if (m.error) card.append(el('div', { class: 'notice error', text: m.error }));
    const meta = el('div', { class: 'meta' });
    const kv = (k, v) => meta.append(el('div', {}, [el('div', { class: 'k', text: k }), el('div', { text: v || '–' })]));
    kv('Started', fmtDateTime(m.started_at)); kv('Ended', fmtDateTime(m.ended_at)); kv('Duration', durationText(m.duration_s));
    kv('App', m.app); kv('Backend', m.backend); kv('Model', m.model); kv('Language', m.language); kv('Words', fmtInt(st.words_total));
    const tt = st.talk_time_s_by_speaker || {};
    if (Object.keys(tt).length) kv('Talk time', Object.entries(tt).map(([k, v]) => `${k} ${durationText(v)}`).join(' · '));
    card.append(meta);
    const exp = (fmt) => el('a', { class: 'btn small', href: `/api/meetings/${encodeURIComponent(m.id)}/export?fmt=${fmt}`, target: '_blank', rel: 'noopener' }, `Export .${fmt}`);
    card.append(el('div', { class: 'detail-actions' }, [exp('md'), exp('txt'), exp('json'), el('span', { class: 'spacer', style: { flex: 1 } }),
      el('button', { type: 'button', class: 'btn small danger', onclick: () => this.remove(m) }, 'Delete')]));
    // Transcript timeline with find box
    const segs = m.transcript || [];
    const find = el('input', { type: 'search', placeholder: 'Find in transcript…', 'aria-label': 'Find in transcript' });
    const count = el('span', { class: 'small muted' });
    const list = el('div', { class: 'transcript' });
    const draw = (needle) => {
      clear(list);
      let hits = 0;
      const re = needle ? new RegExp(needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'ig') : null;
      for (const s of segs) {
        const who = (s.speaker || '').toLowerCase() === 'you' ? 'you' : 'others';
        const line = el('div', { class: `seg-line ${who}` }, [el('span', { class: 't', text: hhmmss(s.start) }), el('span', { class: 'sp', text: s.speaker || who })]);
        const tx = el('span', { class: 'tx' });
        const text = s.text || '';
        if (re && re.test(text)) {
          hits++; line.classList.add('hit');
          let last = 0; re.lastIndex = 0; let mt;
          while ((mt = re.exec(text))) { tx.append(text.slice(last, mt.index), el('mark', { text: mt[0] })); last = mt.index + mt[0].length; if (!mt[0].length) re.lastIndex++; }
          tx.append(text.slice(last));
        } else tx.textContent = text;
        line.append(tx); list.append(line);
      }
      count.textContent = needle ? `${hits} matching segment${hits === 1 ? '' : 's'}` : `${segs.length} segments`;
      if (needle && hits) { const first = list.querySelector('.hit'); if (first) first.scrollIntoView({ block: 'nearest' }); }
    };
    find.addEventListener('input', debounce(() => draw(find.value.trim()), 200));
    card.append(el('div', { class: 'card-head', style: { marginTop: '0.4rem' } }, [el('h3', { text: 'Transcript' }), count, find]));
    if (!segs.length) card.append(el('p', { class: 'small muted', text: m.status === 'recording' ? 'Nothing transcribed yet.' : 'No transcript.' }));
    else { draw(''); card.append(list); }
    box.append(card);
  },

  async remove(m) {
    if (!window.confirm(`Delete “${m.title || 'Untitled meeting'}” and its transcript? This cannot be undone.`)) return;
    try {
      await api('DELETE', `/api/meetings/${encodeURIComponent(m.id)}`);
      this.selected = null; clear($('#meet-detail'));
      toast('Meeting deleted');
      this.load();
    } catch (e) { toast(errText(e), { kind: 'error' }); }
  },

  /* Record / Stop button follows /api/status.meeting */
  syncRecordButton() {
    const btn = $('#meet-record'), live = $('#meet-live');
    if (!btn) return;
    const s = lastStatus;
    const rec = s && s.meeting && s.meeting.recording;
    btn.textContent = rec ? '■ Stop' : '● Record';
    btn.className = `btn ${rec ? '' : 'record'}`;
    const off = !s || !s.running || this.unavailable;
    btn.disabled = off || this.busy;
    btn.title = off ? 'The WhisperLocal app is not running, so meetings cannot be recorded from here.' : rec ? 'Stop recording this meeting' : 'Start recording the current call';
    if (rec) {
      const mt = s.meeting;
      live.textContent = `Recording ${mt.title ? '“' + mt.title + '” ' : ''}${mmss(mt.elapsed_s)}${mt.segments_total ? ` · ${mt.segments_done}/${mt.segments_total} segments` : ''}`;
    } else if (s && s.meeting && s.meeting.status === 'transcribing') live.textContent = 'Transcribing the last meeting…';
    else live.textContent = '';
  },

  async toggleRecord() {
    const rec = lastStatus && lastStatus.meeting && lastStatus.meeting.recording;
    this.busy = true; this.syncRecordButton();
    try {
      if (rec) { await api('POST', '/api/meetings/stop'); toast('Meeting stopped — transcribing.'); }
      else {
        const title = window.prompt('Meeting title (optional):', '');
        if (title === null) return;
        await api('POST', '/api/meetings/start', title ? { title } : {});
        toast('Recording meeting');
      }
      await pollStatus();
      setTimeout(() => this.load(), 1500);
    } catch (e) { toast(errText(e), { kind: 'error' }); }
    finally { this.busy = false; this.syncRecordButton(); }
  },
};
function meetingBadge(s) {
  const cls = s === 'done' || s === 'ok' ? 'ok' : s === 'failed' || s === 'error' ? 'bad' : s === 'recording' || s === 'transcribing' ? 'busy' : '';
  return el('span', { class: `badge ${cls}`, text: s || '–' });
}
$('#meet-record').addEventListener('click', () => meetings.toggleRecord());

/* ─── 7. Settings ────────────────────────────────────────────────────────── */

const TIER_LABEL = { live: 'applies live', listeners: 'restarts listeners', restart: 'needs restart' };
const CUSTOM = 'custom';
const MOUSE_TOKENS = ['mouse_left', 'mouse_right', 'mouse_middle'];
const settingsState = { data: null, dirty: {}, rows: {}, capture: null };
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

const settings = {
  async load() {
    const form = $('#set-form');
    if (!settingsState.data) clear(form).append(el('div', { class: 'empty', text: 'Loading…' }));
    try {
      settingsState.data = await api('GET', '/api/settings');
      settingsState.dirty = {};
      this.render();
    } catch (e) {
      if (e.status === 401) return;
      clear(form).append(el('div', { class: 'notice error', text: `Could not load settings: ${errText(e)}` }));
    }
  },

  /** Current (possibly unsaved) value of a field. */
  cur(name) { return name in settingsState.dirty ? settingsState.dirty[name] : settingsState.data.values[name]; },

  set(name, value) {
    const orig = settingsState.data.values[name];
    if (same(orig, value)) delete settingsState.dirty[name]; else settingsState.dirty[name] = value;
    const row = settingsState.rows[name];
    if (row) row.classList.toggle('dirty', name in settingsState.dirty);
    this.updateSavebar();
    this.updateVisibility();
  },

  updateSavebar() {
    const n = Object.keys(settingsState.dirty).length;
    $('#savebar').hidden = !(n && currentTab === 'settings');
    $('#savebar-count').textContent = `${n} unsaved change${n === 1 ? '' : 's'}`;
  },

  /** Conditional rows: mouse thresholds, API fields, overlay position. */
  updateVisibility() {
    const rows = settingsState.rows, show = (name, on) => { if (rows[name]) rows[name].hidden = !on; };
    const keys = this.cur('trigger_keys') || [];
    const anyMouse = keys.some((k) => MOUSE_TOKENS.includes(k));
    show('mouse_hold_threshold', anyMouse);
    show('mouse_drag_cancel_px', keys.includes('mouse_left'));
    const note = $('#drag-note');
    if (note) note.hidden = !keys.includes('mouse_left');
    const apiOn = this.cur('dictation_backend') === 'api' || this.cur('meeting_backend') === 'api';
    for (const n of ['api_base_url', 'api_model', 'api_key', 'api_timeout_seconds']) show(n, apiOn);
    const overlay = this.cur('overlay');
    for (const n of ['overlay_anchor', 'overlay_offset_x', 'overlay_offset_y']) show(n, overlay !== false);
  },

  render() {
    const d = settingsState.data, form = clear($('#set-form'));
    settingsState.rows = {};
    $('#set-path').textContent = d.config_path || '';
    $('#set-error').hidden = true;
    const notice = $('#set-notice');
    notice.hidden = d.running !== false;
    if (d.running === false) { notice.className = 'notice warn'; notice.textContent = 'The WhisperLocal app is not running. Changes are written to the config file and picked up the next time it starts.'; }
    for (const group of d.schema || []) {
      const card = el('section', { class: 'card settings-group', 'aria-labelledby': `g-${group.key}` }, [
        el('h3', { id: `g-${group.key}`, text: group.label || group.title }), group.help ? el('p', { class: 'card-help', text: group.help }) : null]);
      for (const f of group.fields) card.append(this.row(f));
      form.append(card);
    }
    form.append(el('div', { style: { height: '1rem' } }));
    this.updateVisibility();
    this.updateSavebar();
  },

  row(f) {
    const d = settingsState.data, env = d.sources && d.sources[f.name] === 'env';
    const id = `f-${f.name}`;
    const row = el('div', { class: `frow ${env ? 'env' : ''}`, dataset: { field: f.name } });
    settingsState.rows[f.name] = row;
    const labelTag = ['bool', 'radio', 'multiselect', 'triggers', 'color', 'secret'].includes(f.type) ? 'span' : 'label';
    row.append(el(labelTag, { class: 'flabel', for: labelTag === 'label' ? id : null, id: `${id}-label`, text: f.label }));
    const ctl = el('div', { class: 'fctl' });
    ctl.append(this.control(f, id, env));
    if (f.tier) ctl.append(el('span', { class: `chip tier-${f.tier}`, text: TIER_LABEL[f.tier] || f.tier }));
    if (env) ctl.append(el('span', { class: 'chip env', text: `set by WHISPERLOCAL_${f.name.toUpperCase()}` }));
    row.append(ctl);
    if (f.help) row.append(el('div', { class: 'fhelp', id: `${id}-help`, text: f.help }));
    if (f.warning) row.append(el('div', { class: 'fwarn', text: f.warning }));
    return row;
  },

  control(f, id, env) {
    const value = this.cur(f.name), dis = env || null;
    const help = `${id}-help`;
    switch (f.type) {
      case 'bool': {
        const input = el('input', { type: 'checkbox', id, checked: !!value || null, disabled: dis, 'aria-describedby': help, 'aria-labelledby': `${id}-label`, onchange: () => this.set(f.name, input.checked) });
        return el('label', { class: 'switch' }, [input, el('span', { class: 'track' })]);
      }
      case 'int': case 'number': {
        const input = el('input', { type: 'number', id, value: value === null || value === undefined ? '' : value, min: f.min, max: f.max, step: f.step || (f.type === 'int' ? 1 : 'any'), disabled: dis, 'aria-describedby': help,
          onchange: () => { if (input.value === '') return; const n = f.type === 'int' ? parseInt(input.value, 10) : parseFloat(input.value); if (!Number.isNaN(n)) this.set(f.name, n); } });
        return input;
      }
      case 'text': {
        const input = el('input', { type: 'text', id, value: value === null || value === undefined ? '' : value, placeholder: f.placeholder || '', disabled: dis, 'aria-describedby': help, style: { width: '24rem', maxWidth: '100%' },
          onchange: () => this.set(f.name, input.value.trim()) });
        return input;
      }
      case 'select': return this.selectControl(f, id, env);
      case 'radio': {
        const box = el('div', { class: 'radios', role: 'radiogroup', 'aria-labelledby': `${id}-label`, 'aria-describedby': help });
        for (const o of normOpts(f.options)) {
          const r = el('input', { type: 'radio', name: f.name, value: o.value, checked: value === o.value || null, disabled: dis, onchange: () => this.set(f.name, o.value) });
          box.append(el('label', {}, [r, o.label]));
        }
        return box;
      }
      case 'multiselect': {
        const box = el('div', { class: 'checks', role: 'group', 'aria-labelledby': `${id}-label`, 'aria-describedby': help });
        const order = normOpts(f.options).map((o) => o.value);
        for (const o of normOpts(f.options)) {
          const c = el('input', { type: 'checkbox', value: o.value, checked: (value || []).includes(o.value) || null, disabled: dis,
            onchange: () => { const set = new Set(this.cur(f.name) || []); if (c.checked) set.add(o.value); else set.delete(o.value); this.set(f.name, order.filter((v) => set.has(v))); } });
          box.append(el('label', {}, [c, o.label]));
        }
        return box;
      }
      case 'triggers': return this.triggersControl(f, id, env);
      case 'secret': return this.secretControl(f, id);
      case 'color': return this.colorControl(f, id, env);
      default: return el('span', { class: 'muted', text: `(unsupported field type ${f.type})` });
    }
  },

  /* select with a trailing "custom…" option that reveals a text input */
  selectControl(f, id, env) {
    const opts = normOpts(f.options), hasCustom = opts.some((o) => o.value === CUSTOM);
    const value = this.cur(f.name);
    const known = opts.some((o) => o.value === value && o.value !== CUSTOM);
    const sel = el('select', { id, disabled: env || null, 'aria-describedby': `${id}-help` }, opts.map((o) => el('option', { value: o.value, text: o.label })));
    sel.value = known ? value : (hasCustom ? CUSTOM : value);
    const custom = el('input', { type: 'text', 'aria-label': `${f.label} (custom value)`, placeholder: 'e.g. mlx-community/whisper-large-v3-mlx', value: known ? '' : (value || ''), disabled: env || null, hidden: !(hasCustom && !known), style: { width: '20rem', maxWidth: '100%' },
      onchange: () => this.set(f.name, custom.value.trim()) });
    sel.addEventListener('change', () => {
      if (sel.value === CUSTOM) { custom.hidden = false; custom.focus(); if (custom.value.trim()) this.set(f.name, custom.value.trim()); }
      else { custom.hidden = true; this.set(f.name, sel.value); }
    });
    return el('span', { class: 'fctl' }, [sel, custom]);
  },

  /* trigger chips + add dropdown + "Record key…" capture */
  triggersControl(f, id, env) {
    const d = settingsState.data, catalog = d.triggers || [];
    const info = (tok) => catalog.find((t) => t.token === tok) || { token: tok, label: tok.toUpperCase(), risky: false };
    const wrap = el('div', { style: { display: 'flex', flexDirection: 'column', gap: '0.5rem', width: '100%' } });
    const chips = el('div', { class: 'trigger-chips', role: 'list', 'aria-labelledby': `${id}-label` });
    const sel = el('select', { 'aria-label': 'Add a trigger', disabled: env || null });
    const setKeys = (keys) => { this.set(f.name, keys); draw(); };
    const draw = () => {
      const keys = this.cur(f.name) || [];
      clear(chips);
      if (!keys.length) chips.append(el('span', { class: 'small muted', text: 'No trigger — add one below.' }));
      for (const tok of keys) {
        const t = info(tok);
        chips.append(el('span', { class: 'tchip', role: 'listitem' }, [
          t.label,
          t.risky ? el('span', { class: 'warn-ico', title: riskyText(tok), 'aria-label': 'Warning: ' + riskyText(tok), text: '⚠' }) : null,
          env ? null : el('button', { type: 'button', 'aria-label': `Remove ${t.label}`, onclick: () => setKeys(keys.filter((k) => k !== tok)) }, '×'),
        ]));
      }
      clear(sel).append(el('option', { value: '', text: 'Add a trigger…' }));
      for (const t of catalog) if (!keys.includes(t.token)) sel.append(el('option', { value: t.token, text: t.label + (t.risky ? ' ⚠' : '') }));
    };
    sel.addEventListener('change', () => { if (sel.value) { setKeys([...(this.cur(f.name) || []), sel.value]); } });
    const line = el('span', { class: 'capture-line', id: 'capture-line', 'aria-live': 'polite' });
    const recBtn = el('button', { type: 'button', class: 'btn small', disabled: env || null, onclick: () => this.startCapture(f.name, recBtn, line, (tok) => { const keys = this.cur(f.name) || []; if (!keys.includes(tok)) setKeys([...keys, tok]); }) }, 'Record key…');
    const note = el('div', { class: 'small', id: 'drag-note', hidden: true, text: 'The left mouse button is also what every drag, text selection and window move holds down. The mouse hold threshold and the drag guard below keep ordinary clicks from recording; presses that travel further than the guard are cancelled.' });
    wrap.append(chips, el('div', { class: 'fctl' }, [sel, recBtn, line]), note);
    draw();
    return wrap;
  },

  async startCapture(field, btn, line, onToken) {
    if (settingsState.capture) return this.cancelCapture();
    const setLine = (children) => clear(line).append(...[].concat(children));
    const cancel = el('button', { type: 'button', class: 'btn small ghost', onclick: () => this.cancelCapture() }, 'Cancel');
    const stop = (msg) => {
      if (settingsState.capture) { clearInterval(settingsState.capture.timer); document.removeEventListener('keydown', settingsState.capture.onKey); }
      settingsState.capture = null; btn.textContent = 'Record key…';
      setLine(msg ? [el('span', { text: msg })] : []);
      if (msg) setTimeout(() => { if (!settingsState.capture) clear(line); }, 6000);
    };
    try {
      const r = await api('POST', '/api/settings/capture-key');
      const onKey = (e) => { if (e.key === 'Escape') { e.preventDefault(); this.cancelCapture(); } };
      document.addEventListener('keydown', onKey);
      settingsState.capture = { timer: null, onKey, stop };
      btn.textContent = 'Listening…';
      setLine([el('span', { class: 'spinner' }), el('span', { text: `Press a key or hold a mouse button… (${Math.round(r.seconds_left || 0)}s)` }), cancel]);
      settingsState.capture.timer = setInterval(async () => {
        try {
          const s = await api('GET', '/api/settings/capture-key');
          if (s.state === 'waiting') { setLine([el('span', { class: 'spinner' }), el('span', { text: `Press a key or hold a mouse button… (${Math.round(s.seconds_left || 0)}s)` }), cancel]); return; }
          if (s.state === 'captured') { onToken(s.token); stop(`Added ${s.label || s.token}${s.risky ? ' — ' + riskyText(s.token) : ''}`); return; }
          if (s.state === 'unsupported') { stop(`'${s.pressed || '?'}' can't be a trigger — choose one of the listed keys`); return; }
          if (s.state === 'timeout') { stop('No key pressed — timed out.'); return; }
          if (s.state === 'cancelled') { stop('Cancelled.'); return; }
          if (s.state === 'idle') { stop(''); }
        } catch (e) { stop(errText(e)); }
      }, 250);
    } catch (e) { stop(e.status === 503 ? 'Key capture needs the running app.' : errText(e)); }
  },
  async cancelCapture() {
    const c = settingsState.capture;
    if (!c) return;
    c.stop('Cancelled.');
    try { await api('DELETE', '/api/settings/capture-key'); } catch (e) { /* already gone */ }
  },

  /* API key: masked input + Save key + Remove + "key stored" badge; never displayed */
  secretControl(f, id) {
    const d = settingsState.data;
    const badge = el('span', { class: `badge ${d.api_key_set ? 'ok' : ''}`, text: d.api_key_set ? 'key stored' : 'no key stored' });
    const input = el('input', { type: 'password', id, autocomplete: 'off', placeholder: d.api_key_set ? '••••••••  (leave blank to keep)' : 'sk-…', 'aria-label': f.label, 'aria-describedby': `${id}-help`, style: { width: '20rem', maxWidth: '100%' } });
    const save = el('button', { type: 'button', class: 'btn small', onclick: async () => {
      const key = input.value.trim(); if (!key) return;
      save.disabled = true;
      try { const r = await api('POST', '/api/apikey', { key }); d.api_key_set = !!r.api_key_set; input.value = ''; badge.textContent = 'key stored'; badge.className = 'badge ok'; toast('API key saved to the Keychain'); }
      catch (e) { toast(errText(e), { kind: 'error' }); } finally { save.disabled = false; }
    } }, 'Save key');
    const remove = el('button', { type: 'button', class: 'btn small danger', onclick: async () => {
      if (!window.confirm('Remove the stored API key?')) return;
      try { await api('DELETE', '/api/apikey'); d.api_key_set = false; badge.textContent = 'no key stored'; badge.className = 'badge'; toast('API key removed'); }
      catch (e) { toast(errText(e), { kind: 'error' }); }
    } }, 'Remove');
    return el('span', { class: 'fctl' }, [input, save, remove, badge]);
  },

  /* icon_color: [] = follow menu bar; [r,g,b] floats 0..1 <-> hex */
  colorControl(f, id, env) {
    const value = this.cur(f.name);
    const fixed = Array.isArray(value) && value.length === 3;
    const toHex = (rgb) => '#' + rgb.map((v) => Math.round(Math.max(0, Math.min(1, v)) * 255).toString(16).padStart(2, '0')).join('');
    const fromHex = (hex) => [1, 3, 5].map((i) => Math.round(parseInt(hex.slice(i, i + 2), 16) / 255 * 1000) / 1000);
    const picker = el('input', { type: 'color', id, value: fixed ? toHex(value) : '#ff9400', disabled: env || null, hidden: !fixed, 'aria-label': 'Icon colour', onchange: () => this.set(f.name, fromHex(picker.value)) });
    const follow = el('input', { type: 'checkbox', checked: !fixed || null, disabled: env || null, 'aria-describedby': `${id}-help`,
      onchange: () => { picker.hidden = follow.checked; this.set(f.name, follow.checked ? [] : fromHex(picker.value)); } });
    return el('span', { class: 'fctl' }, [el('label', {}, [follow, ' Follow menu bar (recommended)']), picker]);
  },

  async save() {
    const changes = { ...settingsState.dirty };
    if (!Object.keys(changes).length) return;
    const btn = $('#save-btn'), err = $('#set-error');
    btn.disabled = true; err.hidden = true;
    try {
      const r = await api('PUT', '/api/settings', { changes });
      const ap = r.applied || {}, lines = [];
      const names = (list) => (list || []).map((n) => fieldLabel(n)).join(', ');
      if (ap.live && ap.live.length) lines.push(`Applied live: ${names(ap.live)}`);
      if (ap.listeners && ap.listeners.length) lines.push(`Listeners restarted for: ${names(ap.listeners)}`);
      if (ap.restart && ap.restart.length) lines.push(`Restart needed for: ${names(ap.restart)}`);
      if (r.persisted === false) lines.push('Not written to the config file.');
      for (const w of r.warnings || []) lines.push(`Warning: ${w}`);
      const changed = (r.changed || Object.keys(changes)).length;
      toast(`Saved ${changed} setting${changed === 1 ? '' : 's'}`, {
        kind: r.restart_required ? 'warn' : 'info', lines,
        actions: r.restart_required ? [{ label: 'Restart WhisperLocal', primary: true, onClick: () => restartApp() }, { label: 'Later', onClick: () => {} }] : [],
      });
      settingsState.dirty = {};
      await this.load();
    } catch (e) {
      if (e.status === 401) return;
      err.hidden = false; err.textContent = errText(e);
      err.scrollIntoView({ behavior: 'smooth', block: 'center' });
    } finally { btn.disabled = false; }
  },

  discard() { settingsState.dirty = {}; this.render(); },
};

function normOpts(options) {
  return (options || []).map((o) => (typeof o === 'string' ? { value: o, label: o } : { value: o.value, label: o.label === undefined ? String(o.value) : o.label }));
}
function fieldLabel(name) {
  const d = settingsState.data;
  for (const g of (d && d.schema) || []) for (const f of g.fields) if (f.name === name) return f.label;
  return name;
}
function riskyText(tok) {
  if (tok === 'mouse_left') return 'Every drag, text selection and window move holds this button; the hold threshold and drag guard keep ordinary clicks from recording.';
  if (MOUSE_TOKENS.includes(tok)) return 'Holding this button also sends it to whatever app is under the pointer.';
  return 'Holding this key also holds a modifier the rest of macOS is using, so other apps may see the key too.';
}

/** POST /api/restart, then wait for /api/ping to disappear and come back, then reload. */
async function restartApp() {
  const t = toast('Restarting WhisperLocal…', { timeout: 0, lines: ['This page reloads when the app is back.'] });
  try { await api('POST', '/api/restart'); }
  catch (e) { t.remove(); toast(`Restart failed: ${errText(e)}`, { kind: 'error' }); return; }
  const started = Date.now();
  let wentDown = false;
  await sleep(1500);
  while (Date.now() - started < 60000) {
    try {
      const p = await api('GET', '/api/ping');
      if (wentDown && p && p.running !== false) break;
      if (!wentDown && Date.now() - started > 15000) break; // restarted faster than we could notice
    } catch (e) { wentDown = true; }
    await sleep(1000);
  }
  location.reload();
}

$('#save-btn').addEventListener('click', () => settings.save());
$('#discard-btn').addEventListener('click', () => settings.discard());
window.addEventListener('beforeunload', (e) => { if (Object.keys(settingsState.dirty).length) { e.preventDefault(); e.returnValue = ''; } });

/* ─── 8. Boot ────────────────────────────────────────────────────────────── */

(async function boot() {
  applyChartDefaults();
  try { const p = await api('GET', '/api/ping'); if (p && p.version) $('#version').textContent = `v${p.version}`; } catch (e) { /* status pill reports */ }
  await pollStatus();
  setInterval(pollStatus, 3000);
  route();
})();

"""One suite run as one self-contained page.

The page answers four questions in the order somebody actually asks them:

1. **What does this run say?** A verdict block per model — the load the endpoint
   held, the speed one caller got, the aggregate the deployment reached, and whether
   anything failed. Generated from the measurements, not written by hand.
2. **How does it behave as load rises?** The load curves, one line per harness, plus
   the latency–throughput frontier that says what the extra throughput cost.
3. **Do the harnesses agree?** The reason to run three of them. Where they disagree
   by more than a little, the disagreement is the finding.
4. **What exactly was measured?** Every cell, with its status, its raw artifacts,
   and every field no harness could answer.

No external assets: the data is embedded and the charts are SVG built in the page.
A report can be mailed, committed, or opened on a machine with no network — which is
the opposite of the built-in report GuideLLM ships, whose JS and CSS come from a
versioned URL that will not be there forever.
"""

from __future__ import annotations

import html
import json

from typing import Any
from pathlib import Path

from hgrbench import metrics


def _fields() -> dict[str, dict[str, Any]]:
    return {
        field_id: {
            "label": metrics.LABELS.get(field_id, field_id),
            "unit": metrics.UNITS.get(field_id, ""),
            "lower_is_better": field_id in metrics.LOWER_IS_BETTER,
        }
        for field_id in metrics.UNITS
    }


def payload(suite: dict[str, Any]) -> dict[str, Any]:
    """The suite, reshaped into what the page draws.

    Everything the page needs is computed here or in the page, and nothing is
    recomputed from the harness exports — the exports stay in the suite file for
    provenance, and the report reads the normalised readings so that what it shows
    is exactly what the field maps claimed.
    """
    series: list[dict[str, Any]] = []
    harnesses: dict[str, dict[str, Any]] = {}
    for measurement in suite.get("measurements", []):
        name = measurement["harness"]
        entry = harnesses.setdefault(
            name,
            {"name": name, "versions": [], "missing": [], "derived": {}, "notes": []},
        )
        version = measurement.get("harness_version") or "unknown"
        if version not in entry["versions"]:
            entry["versions"].append(version)
        for field_id in measurement.get("missing", []):
            if field_id not in entry["missing"]:
                entry["missing"].append(field_id)
        entry["derived"].update(measurement.get("derived", {}))
        for note in measurement.get("notes", []):
            if note not in entry["notes"] and not note.startswith("harness reported"):
                entry["notes"].append(note)
        series.append(
            {
                "cell_id": measurement["cell_id"],
                "stage": measurement.get("stage", "matrix"),
                "harness": name,
                "model": measurement["model"],
                "concurrency": measurement.get("concurrency", 0),
                "request_rate": measurement.get("request_rate", 0),
                "repeat": measurement.get("repeat", 1),
                "status": measurement.get("status", ""),
                "errors": measurement.get("errors", 0),
                "readings": measurement.get("readings", {}),
                "derived": measurement.get("derived", {}),
                "input_tokens": measurement.get("input_tokens", 0),
                "output_tokens": measurement.get("output_tokens", 0),
                "duration": measurement.get("duration_seconds", 0),
                "artifacts": measurement.get("artifacts_dir", ""),
            }
        )
    return {
        "suite": {
            "run_id": suite.get("run_id", ""),
            "plan": suite.get("plan_name", ""),
            "description": suite.get("plan_description", ""),
            "source": suite.get("plan_source", ""),
            "endpoint": suite.get("endpoint", ""),
            "started_at": suite.get("started_at", ""),
            "finished_at": suite.get("finished_at", ""),
            "arch": (suite.get("host") or {}).get("arch", ""),
            "planned_cells": suite.get("planned_cells", 0),
        },
        "notes": suite.get("notes", []),
        "harnesses": harnesses,
        "models": suite.get("models", []),
        "series": series,
        "knee": suite.get("knee", {}),
        "fields": _fields(),
        "comparable": list(metrics.COMPARABLE),
        "primary": {
            "ttft": metrics.TTFT_P90,
            "per_stream": metrics.OUTPUT_SPEED_PER_STREAM,
            "total": metrics.OUTPUT_SPEED_TOTAL,
            "latency": metrics.E2E_P90,
            "requests": metrics.REQUEST_THROUGHPUT,
            "errors": metrics.ERROR_COUNT,
            "input": metrics.INPUT_TOKENS_MEAN,
        },
    }


def embedded_json(value: Any) -> str:
    """JSON that cannot end the `<script>` element it is embedded in.

    `<`, `>` and `&` are written as JSON escapes, which `JSON.parse` reads back as the
    same characters. Left alone, a note, a model name or an error containing
    `</script>` would close the data block and put the rest of it in the page as
    markup.
    """
    text = json.dumps(value)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render(suite: dict[str, Any], destination: Path) -> Path:
    """Write the page and return where it went."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    body = TEMPLATE.replace("__TITLE__", html.escape(str(suite.get("run_id", "benchmark"))))
    body = body.replace("__DATA__", embedded_json(payload(suite)))
    destination.write_text(body)
    return destination


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>hgrbench — __TITLE__</title>
<style>
  :root { color-scheme: light dark; }
  .viz-root {
    --surface-0: #f7f7f5;
    --surface-1: #fcfcfb;
    --surface-2: #f0efec;
    --border: #e2e1dc;
    --text-primary: #0b0b0b;
    --text-secondary: #52514e;
    --text-muted: #7c7b76;
    --grid: #eceae5;
    --series-1: #2a78d6;
    --series-2: #eb6834;
    --series-3: #1baf7a;
    --series-4: #eda100;
    --good: #008300;
    --warning: #eda100;
    --critical: #e34948;
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) .viz-root {
      --surface-0: #121211;
      --surface-1: #1a1a19;
      --surface-2: #232322;
      --border: #333331;
      --text-primary: #ffffff;
      --text-secondary: #c3c2b7;
      --text-muted: #918f86;
      --grid: #2a2a28;
      --series-1: #3987e5;
      --series-2: #d95926;
      --series-3: #199e70;
      --series-4: #c98500;
      --good: #008300;
      --warning: #c98500;
      --critical: #e66767;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--surface-0); color: var(--text-primary);
    font: 14px/1.55 ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif;
    font-variant-numeric: tabular-nums;
  }
  .wrap { max-width: 1160px; margin: 0 auto; padding: 40px 24px 96px; }
  header h1 { margin: 0 0 4px; font-size: 22px; font-weight: 600; letter-spacing: -0.015em; }
  header p.sub { margin: 0; color: var(--text-secondary); }
  .facts { display: flex; flex-wrap: wrap; gap: 2px 26px; margin: 18px 0 0; font-size: 13px; color: var(--text-secondary); }
  .facts b { color: var(--text-primary); font-weight: 600; }
  .caveats { margin: 18px 0 0; padding: 12px 16px; border-radius: 9px;
             background: var(--surface-1); border: 1px solid var(--border); }
  .caveats ul { margin: 0; padding-left: 18px; color: var(--text-secondary); font-size: 13px; }
  h2 { margin: 46px 0 14px; font-size: 12px; font-weight: 600; text-transform: uppercase;
       letter-spacing: 0.08em; color: var(--text-secondary); }
  h3 { margin: 0; font-size: 15px; font-weight: 600; }
  p.note { margin: 3px 0 0; font-size: 12px; color: var(--text-muted); }
  .card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px;
          padding: 18px 20px 14px; margin-bottom: 16px; }
  .cards { display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(430px, 1fr)); }
  .verdict { display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); }
  .verdict .card { margin: 0; }
  .verdict h3 { display: flex; align-items: baseline; gap: 8px; }
  .verdict .model { font-size: 16px; }
  .lines { margin: 12px 0 0; padding: 0; list-style: none; font-size: 13px; }
  .lines li { display: flex; justify-content: space-between; gap: 12px; padding: 5px 0;
              border-bottom: 1px solid var(--border); }
  .lines li:last-child { border-bottom: 0; }
  .lines .k { color: var(--text-secondary); }
  .lines .v { font-weight: 600; }
  .lines .v small { font-weight: 400; color: var(--text-muted); margin-left: 3px; }
  .pill { font-size: 11px; padding: 2px 7px; border-radius: 99px; border: 1px solid var(--border);
          color: var(--text-secondary); background: var(--surface-2); }
  .pill.good { color: var(--good); border-color: color-mix(in oklab, var(--good) 40%, var(--border)); }
  .pill.bad { color: var(--critical); border-color: color-mix(in oklab, var(--critical) 40%, var(--border)); }
  .legend { display: flex; flex-wrap: wrap; gap: 6px 18px; margin: 12px 0 4px; font-size: 12px; color: var(--text-secondary); }
  .legend span { display: inline-flex; align-items: center; gap: 6px; }
  .swatch { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
  svg { display: block; width: 100%; overflow: visible; }
  .grid-line { stroke: var(--grid); stroke-width: 1; }
  .axis-text { fill: var(--text-muted); font-size: 11px; }
  .axis-title { fill: var(--text-secondary); font-size: 11px; }
  .point-label { fill: var(--text-muted); font-size: 10px; }
  .mark { cursor: default; }
  .mark:hover { opacity: 0.82; }
  .tooltip { position: fixed; pointer-events: none; opacity: 0; transition: opacity 90ms;
             background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px;
             padding: 8px 10px; font-size: 12px; box-shadow: 0 8px 24px rgb(0 0 0 / 0.16); z-index: 20; }
  .tooltip b { display: block; margin-bottom: 3px; }
  .tooltip .k { color: var(--text-secondary); margin-right: 8px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: right; padding: 7px 9px; border-bottom: 1px solid var(--border); white-space: nowrap; }
  th:first-child, td:first-child, th.l, td.l { text-align: left; }
  thead th { font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em;
             color: var(--text-muted); font-weight: 600; }
  tbody tr:last-child td { border-bottom: 0; }
  td.muted, .muted { color: var(--text-muted); }
  .scroll { overflow-x: auto; }
  .spread { font-weight: 600; }
  .spread.wide { color: var(--critical); }
  .spread.some { color: var(--warning); }
  code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
  footer { margin-top: 56px; color: var(--text-muted); font-size: 12px; }
  footer h4 { margin: 20px 0 6px; font-size: 12px; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.06em; }
  footer ul { margin: 0; padding-left: 18px; }
</style>
</head>
<body class="viz-root">
<div class="wrap">
  <header>
    <h1 id="title"></h1>
    <p class="sub" id="subtitle"></p>
    <div class="facts" id="facts"></div>
    <div class="caveats"><ul id="caveats"></ul></div>
  </header>

  <h2>What this run says</h2>
  <div class="verdict" id="verdict"></div>

  <h2>Behaviour under load</h2>
  <div id="curves"></div>

  <h2>Do the harnesses agree?</h2>
  <div class="card" id="agreement"></div>

  <h2>Run to run</h2>
  <div class="card" id="variance"></div>

  <h2>Every measurement</h2>
  <div class="card scroll" id="table"></div>

  <footer id="footer"></footer>
</div>
<div class="tooltip" id="tip"></div>

<script id="data" type="application/json">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('data').textContent);
const F = D.fields, P = D.primary;
const SVGNS = 'http://www.w3.org/2000/svg';
const COLORS = ['var(--series-1)', 'var(--series-2)', 'var(--series-3)', 'var(--series-4)'];
const HARNESSES = Object.keys(D.harnesses).sort();
const colorOf = (harness) => COLORS[HARNESSES.indexOf(harness) % COLORS.length];

const el = (n, a = {}) => { const e = document.createElementNS(SVGNS, n);
  for (const [k, v] of Object.entries(a)) e.setAttribute(k, v); return e; };
const fmt = (v, d = 1) => (v === null || v === undefined || Number.isNaN(v)) ? '—'
  : Math.abs(v) >= 1000 ? v.toLocaleString(undefined, {maximumFractionDigits: 0})
  : v.toLocaleString(undefined, {maximumFractionDigits: d});
// Every string that came from the suite goes through this before it reaches innerHTML.
// Model names, statuses, notes and error text are written by harnesses and plans, not
// by this page, and none of them is markup.
const ESCAPES = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ESCAPES[c]);
const median = (xs) => { if (!xs.length) return null; const s = [...xs].sort((a, b) => a - b);
  const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };

const tip = document.getElementById('tip');
function hoverable(node, title, rows) {
  node.classList.add('mark');
  node.addEventListener('pointerenter', () => {
    tip.innerHTML = '<b>' + esc(title) + '</b>' + rows.filter(Boolean)
      .map(([k, v]) => '<span class="k">' + esc(k) + '</span>' + esc(v)).join('<br>');
    tip.style.opacity = 1;
  });
  node.addEventListener('pointermove', (e) => {
    tip.style.left = Math.min(e.clientX + 14, innerWidth - tip.offsetWidth - 12) + 'px';
    tip.style.top = (e.clientY + 16) + 'px';
  });
  node.addEventListener('pointerleave', () => { tip.style.opacity = 0; });
}

/* ── data shaping ─────────────────────────────────────────────────────────── */
const OK = D.series.filter(s => s.status === 'ok');
const models = D.models.length ? D.models : [...new Set(D.series.map(s => s.model))];

function pointsFor(model, harness, field) {
  const groups = new Map();
  for (const s of OK) {
    if (s.model !== model || s.harness !== harness) continue;
    const value = s.readings[field];
    if (value === undefined) continue;
    const key = s.concurrency;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(value);
  }
  return [...groups.entries()]
    .map(([concurrency, values]) => ({concurrency, value: median(values), n: values.length,
      spread: values.length > 1 ? Math.max(...values) - Math.min(...values) : 0}))
    .sort((a, b) => a.concurrency - b.concurrency);
}

function harnessesWith(model, field) {
  return HARNESSES.filter(h => pointsFor(model, h, field).length > 0);
}

/* ── header ───────────────────────────────────────────────────────────────── */
document.getElementById('title').textContent = D.suite.plan + ' — ' + models.join(' · ');
document.getElementById('subtitle').textContent = D.suite.description;
const facts = [
  ['Run', D.suite.run_id],
  ['Endpoint', D.suite.endpoint],
  ['Started', D.suite.started_at.replace('T', ' ').replace('+00:00', ' UTC')],
  ['Measurements', `${OK.length} of ${D.series.length} usable`],
  ['Harnesses', HARNESSES.map(h => `${h} ${D.harnesses[h].versions.join('/')}`).join(', ')],
  ['Host', D.suite.arch],
];
document.getElementById('facts').innerHTML = facts
  .filter(([, v]) => v).map(([k, v]) => `<div>${esc(k)} <b>${esc(v)}</b></div>`).join('');
const measured = models.filter(m => D.series.some(s => s.model === m));
const unmeasured = models.filter(m => !measured.includes(m));

const caveats = [...D.notes];
if (unmeasured.length) caveats.push(`Not measured in this run: ${unmeasured.join(', ')}. `
  + 'The plan names them; this invocation narrowed to a subset.');
document.getElementById('caveats').innerHTML = caveats.map(n => `<li>${esc(n)}</li>`).join('');

/* ── verdict ──────────────────────────────────────────────────────────────── */
function verdictFor(model) {
  const mine = OK.filter(s => s.model === model);
  const at = (field, pick) => {
    const values = mine.map(s => ({c: s.concurrency, v: s.readings[field]})).filter(x => x.v !== undefined);
    if (!values.length) return null;
    return pick(values);
  };
  const lowest = mine.length ? Math.min(...mine.map(s => s.concurrency)) : 0;
  const solo = mine.filter(s => s.concurrency === lowest);
  const soloSpeed = median(solo.map(s => s.readings[P.per_stream]).filter(v => v !== undefined));
  const soloTtft = median(solo.map(s => s.readings[P.ttft]).filter(v => v !== undefined));
  const peak = at(P.total, vs => vs.reduce((a, b) => (b.v > a.v ? b : a)));
  const errors = mine.reduce((sum, s) => sum + (s.errors || 0), 0);
  const failed = D.series.filter(s => s.model === model && s.status !== 'ok').length;
  const knee = D.knee[model];
  const rows = [];
  if (soloTtft !== null) rows.push(['TTFT p90 at c' + lowest, fmt(soloTtft, 0) + '<small>ms</small>']);
  if (soloSpeed !== null) rows.push(['One caller gets', fmt(soloSpeed, 0) + '<small>tok/s</small>']);
  if (peak) rows.push(['Best aggregate seen', fmt(peak.v, 0) + '<small>tok/s at c' + peak.c + '</small>']);
  const worstTtft = at(P.ttft, vs => vs.reduce((a, b) => (b.v > a.v ? b : a)));
  if (worstTtft) rows.push(['Worst TTFT p90', fmt(worstTtft.v, 0) + '<small>ms at c' + worstTtft.c + '</small>']);
  if (knee) {
    rows.push([`Holds ${esc(knee.field)} ≤ ${fmt(knee.limit, 0)}`,
      knee.max_passing ? 'up to c' + knee.max_passing : 'at no rung tried']);
    if (knee.breach) rows.push(['Broke at c' + knee.breach.concurrency, `<span class="muted">${esc(knee.breach.why)}</span>`]);
  }
  rows.push(['Failed requests', errors ? `<span class="pill bad">${errors}</span>` : '<span class="pill good">none</span>']);
  if (failed) rows.push(['Cells that did not produce a result', `<span class="pill bad">${failed}</span>`]);
  return rows;
}

document.getElementById('verdict').innerHTML = measured.map(model => `
  <div class="card">
    <h3><span class="model">${esc(model)}</span></h3>
    <ul class="lines">${verdictFor(model).map(([k, v]) =>
      `<li><span class="k">${k}</span><span class="v">${v}</span></li>`).join('')}</ul>
  </div>`).join('');

/* ── charts ───────────────────────────────────────────────────────────────── */
function lineChart(host, {model, field, title, note, unit}) {
  const card = document.createElement('div');
  card.className = 'card';
  const names = harnessesWith(model, field);
  card.innerHTML = `<h3>${title} <span class="muted" style="font-weight:400">${unit}</span></h3><p class="note">${note}</p>
    <div class="legend">${names.map(h =>
      `<span><i class="swatch" style="background:${colorOf(h)}"></i>${esc(h)}</span>`).join('')}</div>`;
  const svg = el('svg');
  card.appendChild(svg);
  host.appendChild(card);
  if (!names.length) { card.innerHTML += '<p class="note">No harness reported this field.</p>'; return; }

  const series = names.map(h => ({harness: h, points: pointsFor(model, h, field)}));
  const xs = [...new Set(series.flatMap(s => s.points.map(p => p.concurrency)))].sort((a, b) => a - b);
  const maxY = Math.max(...series.flatMap(s => s.points.map(p => p.value))) * 1.12 || 1;
  const w = 620, h = 250;
  const padL = 56, padR = 22, padT = 12, padB = 40;
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  svg.setAttribute('height', h);
  const xAt = (c) => padL + (xs.length === 1 ? (w - padL - padR) / 2 : (xs.indexOf(c) / (xs.length - 1)) * (w - padL - padR));
  const yAt = (v) => h - padB - (v / maxY) * (h - padT - padB);

  for (let i = 0; i <= 4; i++) {
    const v = (maxY / 4) * i;
    svg.appendChild(el('line', {class: 'grid-line', x1: padL, x2: w - padR, y1: yAt(v), y2: yAt(v)}));
    const t = el('text', {class: 'axis-text', x: padL - 8, y: yAt(v) + 4, 'text-anchor': 'end'});
    t.textContent = fmt(v, 0); svg.appendChild(t);
  }
  for (const c of xs) {
    const t = el('text', {class: 'axis-text', x: xAt(c), y: h - 20, 'text-anchor': 'middle'});
    t.textContent = 'c' + c; svg.appendChild(t);
  }
  const xTitle = el('text', {class: 'axis-title', x: (padL + w - padR) / 2, y: h - 2, 'text-anchor': 'middle'});
  xTitle.textContent = 'requests in flight';
  svg.appendChild(xTitle);
  for (const s of series) {
    const pts = s.points.map(p => [xAt(p.concurrency), yAt(p.value)]);
    if (pts.length > 1) {
      svg.appendChild(el('path', {d: 'M' + pts.map(p => p[0] + ' ' + p[1]).join(' L'),
        fill: 'none', stroke: colorOf(s.harness), 'stroke-width': 2, 'stroke-linejoin': 'round'}));
    }
    s.points.forEach((p, i) => {
      const c = el('circle', {cx: pts[i][0], cy: pts[i][1], r: 4.5, fill: colorOf(s.harness),
        stroke: 'var(--surface-1)', 'stroke-width': 2});
      hoverable(c, `${s.harness} · c${p.concurrency}`, [
        [F[field].label, fmt(p.value) + ' ' + F[field].unit],
        p.n > 1 ? ['repeats', `${p.n}, spread ${fmt(p.spread)}`] : null,
      ]);
      svg.appendChild(c);
    });
  }
}

function frontier(host, model) {
  const card = document.createElement('div');
  card.className = 'card';
  const names = harnessesWith(model, P.total).filter(h => pointsFor(model, h, P.ttft).length);
  card.innerHTML = `<h3>What the extra throughput cost <span class="muted" style="font-weight:400">latency against throughput</span></h3>
    <p class="note">Aggregate output rate against the latency one caller felt to get it. Up and to the
    right is capacity; the bend is where queueing starts. Each point is labelled with its concurrency.</p>
    <div class="legend">${names.map(h =>
      `<span><i class="swatch" style="background:${colorOf(h)}"></i>${esc(h)}</span>`).join('')}</div>`;
  const svg = el('svg');
  card.appendChild(svg);
  host.appendChild(card);
  if (!names.length) return;

  const series = names.map(h => {
    const total = pointsFor(model, h, P.total);
    const ttft = new Map(pointsFor(model, h, P.ttft).map(p => [p.concurrency, p.value]));
    return {harness: h, points: total.filter(p => ttft.has(p.concurrency))
      .map(p => ({c: p.concurrency, x: p.value, y: ttft.get(p.concurrency)}))};
  }).filter(s => s.points.length);

  const w = 1100, h = 300;
  const padL = 58, padR = 34, padT = 16, padB = 44;
  const maxX = Math.max(...series.flatMap(s => s.points.map(p => p.x))) * 1.1 || 1;
  const maxY = Math.max(...series.flatMap(s => s.points.map(p => p.y))) * 1.15 || 1;
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  svg.setAttribute('height', h);
  const xAt = (v) => padL + (v / maxX) * (w - padL - padR);
  const yAt = (v) => h - padB - (v / maxY) * (h - padT - padB);

  for (let i = 0; i <= 4; i++) {
    const v = (maxY / 4) * i;
    svg.appendChild(el('line', {class: 'grid-line', x1: padL, x2: w - padR, y1: yAt(v), y2: yAt(v)}));
    const t = el('text', {class: 'axis-text', x: padL - 8, y: yAt(v) + 4, 'text-anchor': 'end'});
    t.textContent = fmt(v, 0); svg.appendChild(t);
  }
  for (let i = 0; i <= 4; i++) {
    const v = (maxX / 4) * i;
    const t = el('text', {class: 'axis-text', x: xAt(v), y: h - 22, 'text-anchor': 'middle'});
    t.textContent = fmt(v, 0); svg.appendChild(t);
  }
  const xt = el('text', {class: 'axis-title', x: (padL + w - padR) / 2, y: h - 4, 'text-anchor': 'middle'});
  xt.textContent = 'output tokens/s, all streams';
  svg.appendChild(xt);
  const yt = el('text', {class: 'axis-title', x: padL - 46, y: padT + 4, 'text-anchor': 'start'});
  yt.textContent = F[P.ttft].label + ', ms';
  svg.appendChild(yt);

  for (const s of series) {
    const sorted = [...s.points].sort((a, b) => a.x - b.x);
    if (sorted.length > 1) {
      svg.appendChild(el('path', {d: 'M' + sorted.map(p => xAt(p.x) + ' ' + yAt(p.y)).join(' L'),
        fill: 'none', stroke: colorOf(s.harness), 'stroke-width': 2, 'stroke-linejoin': 'round'}));
    }
    for (const p of sorted) {
      const dot = el('circle', {cx: xAt(p.x), cy: yAt(p.y), r: 5, fill: colorOf(s.harness),
        stroke: 'var(--surface-1)', 'stroke-width': 2});
      hoverable(dot, `${s.harness} · c${p.c}`, [
        ['aggregate', fmt(p.x, 0) + ' tok/s'],
        [F[P.ttft].label, fmt(p.y, 0) + ' ms'],
      ]);
      svg.appendChild(dot);
      const label = el('text', {class: 'point-label', x: xAt(p.x) + 8, y: yAt(p.y) - 7});
      label.textContent = 'c' + p.c;
      svg.appendChild(label);
    }
  }
}

const curves = document.getElementById('curves');
for (const model of measured) {
  const section = document.createElement('div');
  section.innerHTML = `<h3 style="margin:22px 0 10px">${esc(model)}</h3>`;
  curves.appendChild(section);
  const grid = document.createElement('div');
  grid.className = 'cards';
  curves.appendChild(grid);
  lineChart(grid, {model, field: P.ttft, unit: 'ms',
    title: 'Time to first token as load rises',
    note: 'p90, so this is what a slow request looked like, not an average one. Rising steeply means requests are queueing before the first token.'});
  lineChart(grid, {model, field: P.per_stream, unit: 'tok/s',
    title: 'What one caller gets',
    note: 'Output tokens per second for a single stream. This is the number a user feels, and it falls as the deployment fills.'});
  lineChart(grid, {model, field: P.total, unit: 'tok/s',
    title: 'What the deployment produces',
    note: 'Aggregate output across all streams. It rises while there is headroom and flattens at saturation.'});
  lineChart(grid, {model, field: P.latency, unit: 'ms',
    title: 'End to end latency',
    note: 'p90 of the whole request. Prefill, queueing and the full generation together.'});
  const wide = document.createElement('div');
  curves.appendChild(wide);
  frontier(wide, model);
}

/* ── harness agreement ────────────────────────────────────────────────────── */
function agreement() {
  const host = document.getElementById('agreement');
  const rows = [];
  for (const model of measured) {
    const concurrencies = [...new Set(OK.filter(s => s.model === model).map(s => s.concurrency))].sort((a, b) => a - b);
    for (const c of concurrencies) {
      for (const field of D.comparable) {
        const byHarness = new Map();
        for (const h of HARNESSES) {
          const values = OK.filter(s => s.model === model && s.harness === h && s.concurrency === c)
            .map(s => s.readings[field]).filter(v => v !== undefined);
          if (values.length) byHarness.set(h, median(values));
        }
        if (byHarness.size < 2) continue;
        const values = [...byHarness.values()];
        const lo = Math.min(...values), hi = Math.max(...values);
        const mid = (lo + hi) / 2;
        const spread = mid ? ((hi - lo) / mid) * 100 : 0;
        rows.push({model, c, field, byHarness, spread});
      }
    }
  }
  if (!rows.length) {
    host.innerHTML = `<h3>Nothing to compare</h3><p class="note">Two harnesses have to measure the
      same model at the same load point before they can agree or disagree. Run more than one harness
      over the same load points.</p>`;
    return;
  }
  rows.sort((a, b) => b.spread - a.spread);
  const shown = rows.slice(0, 24);
  const cls = (s) => s > 25 ? 'spread wide' : s > 10 ? 'spread some' : 'spread';
  host.innerHTML = `<h3>Where the harnesses disagree</h3>
    <p class="note">The same model, the same requests in flight, the same field — measured by different
    tools. Spread is the range as a percentage of the mid-point. A wide spread is a finding about the
    tools, not about the endpoint: they build prompts differently, count tokens differently, and define
    inter-token latency differently. ${rows.length > shown.length ? `Showing the ${shown.length} widest of ${rows.length}.` : ''}</p>
    <div class="scroll"><table><thead><tr>
      <th class="l">Model</th><th>Load</th><th class="l">Field</th>
      ${HARNESSES.map(h => `<th>${esc(h)}</th>`).join('')}<th>Spread</th>
    </tr></thead><tbody>
    ${shown.map(r => `<tr>
      <td class="l">${esc(r.model)}</td><td>c${esc(r.c)}</td>
      <td class="l">${esc(F[r.field].label)} <span class="muted">${esc(F[r.field].unit)}</span></td>
      ${HARNESSES.map(h => `<td>${r.byHarness.has(h) ? fmt(r.byHarness.get(h)) : '<span class="muted">—</span>'}</td>`).join('')}
      <td class="${cls(r.spread)}">${fmt(r.spread, 0)}%</td>
    </tr>`).join('')}
    </tbody></table></div>`;
}
agreement();

/* ── repeat variance ──────────────────────────────────────────────────────── */
function variance() {
  const host = document.getElementById('variance');
  const groups = new Map();
  for (const s of OK) {
    const key = [s.model, s.harness, s.concurrency, s.stage].join('|');
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(s);
  }
  const repeated = [...groups.entries()].filter(([, xs]) => xs.length > 1);
  if (!repeated.length) {
    host.innerHTML = `<h3>One pass each</h3><p class="note">This plan ran every cell once, so there is
      no run-to-run spread to show. Raise <code>repeats</code> in the plan to find out how much of a
      difference between two numbers is just noise — on a shared production endpoint it is usually more
      than people expect.</p>`;
    return;
  }
  const field = P.ttft;
  host.innerHTML = `<h3>How much of a difference is noise</h3>
    <p class="note">${esc(F[field].label)} across repeats of the same cell. If two harnesses differ by less
    than one cell's own spread, they do not disagree.</p>
    <div class="scroll"><table><thead><tr>
      <th class="l">Model</th><th class="l">Harness</th><th>Load</th><th>Repeats</th>
      <th>Min</th><th>Median</th><th>Max</th><th>Spread</th>
    </tr></thead><tbody>${repeated.map(([key, xs]) => {
      const [model, harness, c] = key.split('|');
      const values = xs.map(s => s.readings[field]).filter(v => v !== undefined);
      if (!values.length) return '';
      const lo = Math.min(...values), hi = Math.max(...values), mid = median(values);
      const pct = mid ? ((hi - lo) / mid) * 100 : 0;
      return `<tr><td class="l">${esc(model)}</td><td class="l">${esc(harness)}</td><td>c${esc(c)}</td>
        <td>${values.length}</td><td>${fmt(lo, 0)}</td><td>${fmt(mid, 0)}</td><td>${fmt(hi, 0)}</td>
        <td class="${pct > 25 ? 'spread wide' : pct > 10 ? 'spread some' : 'spread'}">${fmt(pct, 0)}%</td></tr>`;
    }).join('')}</tbody></table></div>`;
}
variance();

/* ── the full table ───────────────────────────────────────────────────────── */
function table() {
  const cols = [P.ttft, P.per_stream, P.total, P.latency, P.requests, P.input, P.errors];
  const rows = [...D.series].sort((a, b) =>
    a.model.localeCompare(b.model) || a.concurrency - b.concurrency ||
    a.harness.localeCompare(b.harness) || a.repeat - b.repeat);
  document.getElementById('table').innerHTML = `
    <h3>Every cell, as measured</h3>
    <p class="note">A derived value is marked °. Its formula is in the notes at the foot of the page.
    Raw exports are under each cell's own directory, unconverted.</p>
    <table><thead><tr>
      <th class="l">Model</th><th class="l">Harness</th><th class="l">Stage</th><th>Load</th><th>#</th>
      ${cols.map(c => `<th>${esc(F[c].label)}<br><span class="muted">${esc(F[c].unit)}</span></th>`).join('')}
      <th class="l">Status</th>
    </tr></thead><tbody>
    ${rows.map(s => `<tr>
      <td class="l">${esc(s.model)}</td><td class="l">${esc(s.harness)}</td>
      <td class="l muted">${esc(s.stage)}</td>
      <td>c${esc(s.concurrency)}${s.request_rate ? ` @${fmt(s.request_rate)}/s` : ''}</td>
      <td class="muted">${esc(s.repeat)}</td>
      ${cols.map(c => {
        const v = s.readings[c];
        if (v === undefined) return '<td class="muted">—</td>';
        return `<td>${fmt(v, c === P.errors ? 0 : 1)}${s.derived[c] ? '°' : ''}</td>`;
      }).join('')}
      <td class="l">${s.status === 'ok'
        ? (s.errors ? `<span class="pill bad">${esc(s.errors)} failed</span>` : '<span class="pill good">ok</span>')
        : `<span class="pill bad">${esc(s.status)}</span>`}</td>
    </tr>`).join('')}
    </tbody></table>`;
}
table();

/* ── footnotes ────────────────────────────────────────────────────────────── */
function footer() {
  const parts = [];
  parts.push('<h4>The harnesses in this run</h4><ul>' + HARNESSES.map(h => {
    const entry = D.harnesses[h];
    const missing = entry.missing.length
      ? ` Could not answer: ${entry.missing.map(m => esc(F[m] ? F[m].label : m)).join(', ')}.` : '';
    const derived = Object.keys(entry.derived).length
      ? ' Derived here: ' + Object.entries(entry.derived)
        .map(([k, how]) => `${esc(F[k] ? F[k].label : k)} as ${esc(how)}`).join('; ') + '.' : '';
    return `<li><b>${esc(h)}</b> ${esc(entry.versions.join(', '))}.${missing}${derived}</li>`;
  }).join('') + '</ul>');
  const notes = HARNESSES.flatMap(h => D.harnesses[h].notes.map(n => `<li><b>${esc(h)}</b> — ${esc(n)}</li>`));
  if (notes.length) parts.push('<h4>What each tool did with the request</h4><ul>' + notes.join('') + '</ul>');
  parts.push(`<h4>Provenance</h4><ul>
    <li>Plan: <code>${esc(D.suite.source)}</code></li>
    <li>Run: <code>${esc(D.suite.run_id)}</code>, ${D.series.length} measurements from ${D.suite.planned_cells} planned cells</li>
    <li>Every harness's own export is kept verbatim in <code>suite.json</code> and beside it, so a
    correction to a field map is a re-read rather than a re-run.</li></ul>`);
  document.getElementById('footer').innerHTML = parts.join('');
}
footer();

/* No resize handler: every chart is drawn into a viewBox, so the browser scales it
   with the window. Re-rendering on resize was a reload loop in headless Chrome. */
</script>
</body>
</html>
"""

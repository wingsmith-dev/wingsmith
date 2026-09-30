#!/usr/bin/env python3
"""Render an AIPerf artifact directory as one self-contained HTML page.

Reads the two files AIPerf writes that carry the whole run: the summary export
(`profile_export_aiperf.json`) and the per-request log (`profile_export.jsonl`).
Everything else in the directory is either a copy of those or the harness log.

Usage:
    dev/aiperf_report.py <artifact-dir> [-o report.html]

The output has no external dependencies — the data is embedded and the charts are
SVG built in the page, so the file can be moved or mailed and still render.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AIPerf — __TITLE__</title>
<style>
  :root { color-scheme: light dark; }
  .viz-root {
    --surface-0: #f7f7f5;
    --surface-1: #fcfcfb;
    --border: #e2e1dc;
    --text-primary: #0b0b0b;
    --text-secondary: #52514e;
    --text-muted: #7c7b76;
    --grid: #eceae5;
    --series-1: #2a78d6;
    --series-2: #eb6834;
    --good: #008300;
    --critical: #e34948;
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) .viz-root {
      --surface-0: #121211;
      --surface-1: #1a1a19;
      --border: #333331;
      --text-primary: #ffffff;
      --text-secondary: #c3c2b7;
      --text-muted: #918f86;
      --grid: #2a2a28;
      --series-1: #3987e5;
      --series-2: #d95926;
      --good: #008300;
      --critical: #e66767;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--surface-0);
    color: var(--text-primary);
    font: 14px/1.5 ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif;
    font-variant-numeric: tabular-nums;
  }
  .wrap { max-width: 1080px; margin: 0 auto; padding: 40px 24px 72px; }
  header h1 { margin: 0 0 4px; font-size: 21px; letter-spacing: -0.01em; font-weight: 600; }
  header p { margin: 0; color: var(--text-secondary); }
  .caveat {
    margin: 20px 0 0; padding: 10px 14px; border-radius: 8px;
    background: var(--surface-1); border: 1px solid var(--border);
    color: var(--text-secondary); font-size: 13px;
  }
  .facts { display: flex; flex-wrap: wrap; gap: 4px 28px; margin: 20px 0 0; font-size: 13px; }
  .facts div { color: var(--text-secondary); }
  .facts b { color: var(--text-primary); font-weight: 600; }
  h2 { margin: 44px 0 14px; font-size: 13px; font-weight: 600;
       text-transform: uppercase; letter-spacing: 0.07em; color: var(--text-secondary); }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(158px, 1fr)); gap: 12px; }
  .tile { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }
  .tile .label { font-size: 12px; color: var(--text-secondary); min-height: 2lh; }
  .tile .value { font-size: 26px; font-weight: 600; letter-spacing: -0.02em; margin-top: 4px; }
  .tile .unit { font-size: 13px; font-weight: 400; color: var(--text-secondary); margin-left: 3px; }
  .tile .sub { font-size: 12px; color: var(--text-muted); margin-top: 2px; }
  .card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 18px 20px 12px; }
  .card h3 { margin: 0; font-size: 15px; font-weight: 600; }
  .card p.note { margin: 3px 0 0; font-size: 12px; color: var(--text-muted); }
  .legend { display: flex; gap: 18px; margin: 12px 0 6px; font-size: 12px; color: var(--text-secondary); }
  .legend span { display: inline-flex; align-items: center; gap: 6px; }
  .swatch { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
  svg { display: block; width: 100%; overflow: visible; }
  .grid-line { stroke: var(--grid); stroke-width: 1; }
  .axis-text { fill: var(--text-muted); font-size: 11px; }
  .row-label { fill: var(--text-secondary); font-size: 11px; }
  .mark:hover { opacity: 0.82; }
  .tooltip {
    position: fixed; pointer-events: none; opacity: 0; transition: opacity 90ms;
    background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px;
    padding: 8px 10px; font-size: 12px; box-shadow: 0 6px 20px rgb(0 0 0 / 0.14); z-index: 10;
  }
  .tooltip b { display: block; margin-bottom: 3px; }
  .tooltip .k { color: var(--text-secondary); margin-right: 8px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: right; padding: 7px 10px; border-bottom: 1px solid var(--border); }
  th:first-child, td:first-child { text-align: left; }
  thead th { font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em;
             color: var(--text-muted); font-weight: 600; }
  tbody tr:last-child td { border-bottom: 0; }
  td.metric { color: var(--text-primary); }
  td.unit { color: var(--text-muted); text-align: left; }
  code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
  .cmd { margin-top: 12px; color: var(--text-secondary); word-break: break-all; line-height: 1.6; }
</style>
</head>
<body class="viz-root">
<div class="wrap">
  <header>
    <h1>__MODEL__ — AIPerf __VERSION__</h1>
    <p id="subtitle"></p>
    <div class="facts" id="facts"></div>
    <p class="caveat" id="caveat"></p>
  </header>

  <h2>Headline</h2>
  <div class="tiles" id="tiles"></div>

  <h2>Per request</h2>
  <div class="card">
    <h3>Where each request spent its time</h3>
    <p class="note">Time to first token, then decode, in milliseconds. One bar per request, in the order they started.</p>
    <div class="legend">
      <span><i class="swatch" style="background:var(--series-1)"></i>Time to first token</span>
      <span><i class="swatch" style="background:var(--series-2)"></i>Decode</span>
    </div>
    <svg id="chart-requests"></svg>
  </div>

  <h2>Latency profile</h2>
  <div class="card">
    <h3>Percentiles, milliseconds</h3>
    <p class="note" id="percentile-note"></p>
    <div class="legend">
      <span><i class="swatch" style="background:var(--series-1)"></i>Time to first token</span>
      <span><i class="swatch" style="background:var(--series-2)"></i>Request latency</span>
    </div>
    <svg id="chart-percentiles"></svg>
  </div>

  <h2>Every metric</h2>
  <div class="card">
    <table id="metrics-table">
      <thead><tr><th>Metric</th><th>Unit</th><th>avg</th><th>min</th><th>p50</th><th>p90</th><th>p99</th><th>max</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>

  <p class="cmd"><code id="cli"></code></p>
</div>
<div class="tooltip" id="tip"></div>

<script id="data" type="application/json">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('data').textContent);
const SVGNS = 'http://www.w3.org/2000/svg';
const el = (n, a = {}) => { const e = document.createElementNS(SVGNS, n);
  for (const [k, v] of Object.entries(a)) e.setAttribute(k, v); return e; };
const fmt = (v, d = 1) => v == null ? '—'
  : Math.abs(v) >= 1000 ? v.toLocaleString(undefined, {maximumFractionDigits: 0})
  : v.toLocaleString(undefined, {maximumFractionDigits: d});

const tip = document.getElementById('tip');
function hoverable(node, rows, title) {
  node.classList.add('mark');
  node.addEventListener('pointerenter', () => {
    tip.innerHTML = '<b>' + title + '</b>' + rows
      .map(([k, v]) => '<span class="k">' + k + '</span>' + v).join('<br>');
    tip.style.opacity = 1;
  });
  node.addEventListener('pointermove', (e) => {
    tip.style.left = Math.min(e.clientX + 14, innerWidth - tip.offsetWidth - 12) + 'px';
    tip.style.top = (e.clientY + 16) + 'px';
  });
  node.addEventListener('pointerleave', () => { tip.style.opacity = 0; });
}

/* Header */
document.getElementById('subtitle').textContent =
  D.requests.length + ' requests at concurrency ' + D.config.concurrency +
  ' · ' + D.config.endpoint + ' · ' + new Date(D.start_time).toLocaleString();
const facts = [
  ['Endpoint type', D.config.endpoint_type + (D.config.streaming ? ', streaming' : '')],
  ['Input tokens', fmt(D.config.isl, 0)],
  ['Output tokens asked', fmt(D.config.osl, 0)],
  ['Tokenizer', D.config.tokenizer],
  ['Duration', fmt(D.duration_sec, 2) + ' s'],
  ['Errors', D.errors],
];
document.getElementById('facts').innerHTML = facts
  .map(([k, v]) => '<div>' + k + ' <b>' + v + '</b></div>').join('');
document.getElementById('caveat').textContent = D.caveat;
document.getElementById('cli').textContent = D.cli;

/* Stat tiles */
const tiles = [
  ['Time to first token', D.m.time_to_first_token, 'p50', 'ms', 'p90 __p90__'],
  ['Inter-token latency', D.m.inter_token_latency, 'avg', 'ms', 'p90 __p90__'],
  ['Output tokens / s / user', D.m.output_token_throughput_per_user, 'avg', 'tok/s', null],
  ['Output token throughput', D.m.output_token_throughput, 'avg', 'tok/s', null],
  ['Request throughput', D.m.request_throughput, 'avg', 'req/s', null],
  ['Request latency', D.m.request_latency, 'p50', 'ms', 'p90 __p90__'],
];
document.getElementById('tiles').innerHTML = tiles.filter(([, m]) => m).map(([label, m, stat, unit, sub]) => {
  const v = m[stat] != null ? m[stat] : m.avg;
  const subtext = sub && m.p90 != null ? sub.replace('__p90__', fmt(m.p90)) : '';
  return '<div class="tile"><div class="label">' + label + '</div>' +
    '<div class="value">' + fmt(v) + '<span class="unit">' + unit + '</span></div>' +
    '<div class="sub">' + (stat === 'p50' ? 'median' : 'mean') +
    (subtext ? ' · ' + subtext : '') + '</div></div>';
}).join('');

/* Per-request stacked bars */
function drawRequests() {
  const svg = document.getElementById('chart-requests');
  const rows = D.requests;
  const padL = 34, padR = 8, padT = 8, barH = 16, gap = 8, axisH = 26;
  const h = padT + rows.length * (barH + gap) + axisH;
  const w = svg.clientWidth || 900;
  svg.setAttribute('viewBox', '0 0 ' + w + ' ' + h);
  svg.setAttribute('height', h);
  const max = Math.max(...rows.map(r => r.latency)) * 1.04;
  const x = (v) => padL + (v / max) * (w - padL - padR);

  const ticks = 5;
  for (let i = 0; i <= ticks; i++) {
    const v = (max / ticks) * i;
    svg.appendChild(el('line', {class: 'grid-line', x1: x(v), x2: x(v),
      y1: padT, y2: padT + rows.length * (barH + gap) - gap + 4}));
    const t = el('text', {class: 'axis-text', x: x(v), y: h - 10, 'text-anchor': 'middle'});
    t.textContent = fmt(v, 0);
    svg.appendChild(t);
  }
  const unit = el('text', {class: 'axis-text', x: padL, y: h - 10, 'text-anchor': 'start'});

  rows.forEach((r, i) => {
    const y = padT + i * (barH + gap);
    const label = el('text', {class: 'row-label', x: padL - 8, y: y + barH - 4, 'text-anchor': 'end'});
    label.textContent = '#' + (i + 1);
    svg.appendChild(label);

    const ttft = el('rect', {x: padL, y, width: Math.max(x(r.ttft) - padL, 2), height: barH,
      rx: 3, fill: 'var(--series-1)'});
    hoverable(ttft, [['TTFT', fmt(r.ttft) + ' ms'], ['Output tokens', fmt(r.osl, 0)]],
      'Request #' + (i + 1) + ' — first token');
    svg.appendChild(ttft);

    const decodeW = Math.max(x(r.latency) - x(r.ttft) - 2, 2);
    const dec = el('rect', {x: x(r.ttft) + 2, y, width: decodeW, height: barH,
      rx: 3, fill: 'var(--series-2)'});
    hoverable(dec, [['Decode', fmt(r.latency - r.ttft) + ' ms'],
      ['Inter-token', fmt(r.itl, 2) + ' ms'], ['Total', fmt(r.latency) + ' ms']],
      'Request #' + (i + 1) + ' — decode');
    svg.appendChild(dec);

    const total = el('text', {class: 'row-label', x: x(r.latency) + 6, y: y + barH - 4});
    total.textContent = fmt(r.latency, 0) + ' ms';
    svg.appendChild(total);
  });
  unit.textContent = '';
  svg.appendChild(unit);
}

/* Percentile lines */
function drawPercentiles() {
  const svg = document.getElementById('chart-percentiles');
  const keys = ['p50', 'p75', 'p90', 'p95', 'p99'];
  const series = [
    {name: 'Time to first token', color: 'var(--series-1)', m: D.m.time_to_first_token},
    {name: 'Request latency', color: 'var(--series-2)', m: D.m.request_latency},
  ].filter(s => s.m);
  const w = svg.clientWidth || 900, h = 220;
  const padL = 46, padR = 52, padT = 12, padB = 30;
  svg.setAttribute('viewBox', '0 0 ' + w + ' ' + h);
  svg.setAttribute('height', h);
  const max = Math.max(...series.flatMap(s => keys.map(k => s.m[k] || 0))) * 1.1;
  const x = (i) => padL + (i / (keys.length - 1)) * (w - padL - padR);
  const y = (v) => h - padB - (v / max) * (h - padT - padB);

  for (let i = 0; i <= 4; i++) {
    const v = (max / 4) * i;
    svg.appendChild(el('line', {class: 'grid-line', x1: padL, x2: w - padR, y1: y(v), y2: y(v)}));
    const t = el('text', {class: 'axis-text', x: padL - 8, y: y(v) + 4, 'text-anchor': 'end'});
    t.textContent = fmt(v, 0);
    svg.appendChild(t);
  }
  keys.forEach((k, i) => {
    const t = el('text', {class: 'axis-text', x: x(i), y: h - 10, 'text-anchor': 'middle'});
    t.textContent = k;
    svg.appendChild(t);
  });

  series.forEach((s) => {
    const pts = keys.map((k, i) => [x(i), y(s.m[k] || 0)]);
    svg.appendChild(el('path', {d: 'M' + pts.map(p => p[0] + ' ' + p[1]).join(' L'),
      fill: 'none', stroke: s.color, 'stroke-width': 2, 'stroke-linejoin': 'round'}));
    pts.forEach((p, i) => {
      const c = el('circle', {cx: p[0], cy: p[1], r: 4.5, fill: s.color,
        stroke: 'var(--surface-1)', 'stroke-width': 2});
      hoverable(c, [[keys[i], fmt(s.m[keys[i]]) + ' ms']], s.name);
      svg.appendChild(c);
    });
    const last = pts[pts.length - 1];
    const lab = el('text', {class: 'row-label', x: last[0] + 9, y: last[1] + 4});
    lab.textContent = fmt(s.m.p99, 0) + ' ms';
    svg.appendChild(lab);
  });
}

/* Table */
const tbody = document.querySelector('#metrics-table tbody');
tbody.innerHTML = D.table.map(r =>
  '<tr><td class="metric">' + r.name + '</td><td class="unit">' + r.unit + '</td>' +
  ['avg', 'min', 'p50', 'p90', 'p99', 'max'].map(k => '<td>' + fmt(r[k], 2) + '</td>').join('') +
  '</tr>').join('');

document.getElementById('percentile-note').textContent =
  D.requests.length < 30
    ? 'Only ' + D.requests.length + ' requests — these percentiles are the sample, not a distribution.'
    : 'Across ' + D.requests.length + ' requests.';

function draw() {
  document.getElementById('chart-requests').replaceChildren();
  document.getElementById('chart-percentiles').replaceChildren();
  drawRequests(); drawPercentiles();
}
draw();
addEventListener('resize', draw);
</script>
</body>
</html>
"""

TABLE_ORDER = [
    ("time_to_first_token", "Time to first token"),
    ("time_to_second_token", "Time to second token"),
    ("inter_token_latency", "Inter-token latency"),
    ("inter_chunk_latency", "Inter-chunk latency"),
    ("request_latency", "Request latency"),
    ("output_token_throughput_per_user", "Output tokens/s per user"),
    ("output_token_throughput", "Output token throughput"),
    ("total_token_throughput", "Total token throughput"),
    ("request_throughput", "Request throughput"),
    ("input_sequence_length", "Input sequence length"),
    ("output_sequence_length", "Output sequence length"),
    ("total_output_tokens", "Total output tokens"),
    ("http_req_waiting", "HTTP waiting"),
    ("http_req_receiving", "HTTP receiving"),
    ("http_req_connecting", "HTTP connecting"),
    ("http_req_duration", "HTTP duration"),
    ("benchmark_duration", "Benchmark duration"),
]


def load(artifacts: Path) -> tuple[dict, list[dict]]:
    summary = json.loads((artifacts / "profile_export_aiperf.json").read_text())
    records = []
    jsonl = artifacts / "profile_export.jsonl"
    if jsonl.exists():
        for line in jsonl.read_text().splitlines():
            if line.strip():
                records.append(json.loads(line))
    return summary, records


def build(summary: dict, records: list[dict], source: str) -> str:
    cfg = summary.get("input_config", {})
    endpoint = cfg.get("endpoint", {})
    phase = (cfg.get("phases") or [{}])[0]
    dataset = (cfg.get("datasets") or [{}])[0]
    prompts = dataset.get("prompts", {})
    model = ((cfg.get("models", {}).get("items") or [{}])[0]).get("name", "unknown")

    def metric(key):
        m = summary.get(key)
        return m if isinstance(m, dict) and "unit" in m else None

    requests = []
    for rec in sorted(records, key=lambda r: r["metadata"].get("request_start_ns", 0)):
        recorded = rec.get("metrics") or {}

        def val(key, recorded=recorded):
            v = recorded.get(key)
            return v.get("value") if isinstance(v, dict) else None

        latency = val("request_latency")
        ttft = val("time_to_first_token")
        if latency is None or ttft is None:
            continue
        requests.append({
            "ttft": ttft,
            "latency": latency,
            "itl": val("inter_token_latency") or 0,
            "osl": val("output_sequence_length") or 0,
        })

    table = []
    for key, name in TABLE_ORDER:
        m = metric(key)
        if not m:
            continue
        row = {"name": name, "unit": m.get("unit", "")}
        for stat in ("avg", "min", "p50", "p90", "p99", "max"):
            row[stat] = m.get(stat)
        table.append(row)

    data = {
        "model": model,
        "version": summary.get("aiperf_version", ""),
        "start_time": summary.get("start_time"),
        "duration_sec": (metric("benchmark_duration") or {}).get("avg"),
        "errors": len(summary.get("error_summary") or []),
        "cli": summary.get("run_info", {}).get("cli_command", ""),
        "caveat": (
            "The load generator ran in a container on this machine, so every number "
            "carries the client's own network path to the endpoint. Read it for shape "
            "and for whether the path works — not as the cluster's latency."
        ),
        "config": {
            "endpoint": (endpoint.get("urls") or ["?"])[0],
            "endpoint_type": endpoint.get("type", "?"),
            "streaming": endpoint.get("streaming", False),
            "concurrency": phase.get("concurrency", "?"),
            "isl": prompts.get("isl", {}).get("mean"),
            "osl": prompts.get("osl", {}).get("mean"),
            "tokenizer": cfg.get("tokenizer", {}).get("name", "—"),
            "source": source,
        },
        "m": {k: metric(k) for k, _ in TABLE_ORDER if metric(k)},
        "requests": requests,
        "table": table,
    }

    html = TEMPLATE
    html = html.replace("__TITLE__", f"{model} · {data['start_time'] or ''}")
    html = html.replace("__MODEL__", model)
    html = html.replace("__VERSION__", data["version"])
    return html.replace("__DATA__", json.dumps(data))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", type=Path, help="AIPerf --artifact-dir")
    parser.add_argument("-o", "--out", type=Path, default=None)
    args = parser.parse_args()

    summary, records = load(args.artifacts)
    out = args.out or args.artifacts / "report.html"
    out.write_text(build(summary, records, str(args.artifacts)))
    print(out)


if __name__ == "__main__":
    main()

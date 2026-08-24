"""Turn the collected database into something you can actually look at.

Writes one self-contained HTML file — no internet needed to open it, no
libraries, nothing to install. Regenerate it after each collection run
(`python -m wet.cli export`, or it happens automatically as the last step
of both the local daily job and the GitHub Actions workflow).

Two views, one page:
  Overview  — every show, cheapest to dearest, for comparing across the board.
  By show   — pick one show from a dropdown and see its own price history and
              every upcoming performance.

The page answers the question the whole project exists to answer: "when is
it cheapest to see this show, and is that changing?"
"""

import html
import json
import sqlite3
from collections import defaultdict
from datetime import date, datetime

# --- palette -----------------------------------------------------------------
# Neutrals biased slightly teal so they read as chosen, not defaulted.
# --cheap/--dear are used ONLY as text/pill colours (validated >=4.5:1 contrast
# against both grounds). They are never placed adjacent to --accent as two
# competing hues in the same chart — the earlier version of this page did
# that in the "cheapest week" strip and failed the colour-vision separation
# check (ΔE 5.9, ATG's own bug fixed the same day it shipped). The strip and
# the new trend chart both use single-hue emphasis instead: the same accent
# at two lightnesses/opacities, so there is no pair to confuse.
CSS = """
:root {
  --ground:  #F4F7F6;
  --surface: #FFFFFF;
  --surface-2: #EEF3F2;
  --ink:     #14201F;
  --muted:   #5C6B6A;
  --rule:    #DBE4E2;
  --accent:  #1F6F6B;
  --accent-soft: #BFDAD6;
  --cheap:   #2E7D5B;
  --dear:    #B4472E;
  --warn:    #9A6B12;
  --shadow:  0 1px 2px rgba(20,32,31,.06), 0 8px 24px rgba(20,32,31,.05);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground:  #0E1615;
    --surface: #16211F;
    --surface-2: #1C2926;
    --ink:     #E8EFEE;
    --muted:   #93A5A3;
    --rule:    #26332F;
    --accent:  #4FB3A5;
    --accent-soft: #23423E;
    --cheap:   #5FBF8E;
    --dear:    #E08167;
    --warn:    #D8A94E;
    --shadow:  0 1px 2px rgba(0,0,0,.4), 0 8px 24px rgba(0,0,0,.3);
  }
}
:root[data-theme="dark"] {
  --ground:  #0E1615;
  --surface: #16211F;
  --surface-2: #1C2926;
  --ink:     #E8EFEE;
  --muted:   #93A5A3;
  --rule:    #26332F;
  --accent:  #4FB3A5;
  --accent-soft: #23423E;
  --cheap:   #5FBF8E;
  --dear:    #E08167;
  --warn:    #D8A94E;
  --shadow:  0 1px 2px rgba(0,0,0,.4), 0 8px 24px rgba(0,0,0,.3);
}

* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--ground);
  color: var(--ink);
  font-family: "IBM Plex Sans", ui-sans-serif, system-ui, -apple-system, sans-serif;
  font-size: 16px;
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 1080px; margin: 0 auto; padding: 40px 24px 80px; }

header { border-bottom: 2px solid var(--ink); padding-bottom: 20px; margin-bottom: 22px; }
.eyebrow {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 11px; letter-spacing: .16em; text-transform: uppercase;
  color: var(--accent); margin: 0 0 10px;
}
h1 {
  font-family: "Instrument Serif", Georgia, "Times New Roman", serif;
  font-weight: 400; font-size: clamp(32px, 5vw, 48px); line-height: 1.05;
  margin: 0 0 8px; text-wrap: balance; letter-spacing: -.01em;
}
.sub { color: var(--muted); margin: 0; max-width: 62ch; }

.stats { display: flex; flex-wrap: wrap; gap: 26px 40px; margin: 24px 0 4px; }
.stat .n {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 28px; font-variant-numeric: tabular-nums; line-height: 1.1;
}
.stat .k {
  font-size: 11px; letter-spacing: .12em; text-transform: uppercase;
  color: var(--muted); margin-top: 4px;
}

/* -- tabs ------------------------------------------------------------- */
nav.tabs {
  display: flex; gap: 4px; margin: 26px 0 28px;
  border-bottom: 1px solid var(--rule);
}
nav.tabs button {
  font: inherit; font-size: 14px; font-weight: 600; color: var(--muted);
  background: none; border: none; cursor: pointer;
  padding: 10px 4px; margin-right: 22px; position: relative;
}
nav.tabs button::after {
  content: ""; position: absolute; left: 0; right: 0; bottom: -1px; height: 2px;
  background: transparent; border-radius: 2px 2px 0 0;
}
nav.tabs button:hover { color: var(--ink); }
nav.tabs button[aria-selected="true"] { color: var(--ink); }
nav.tabs button[aria-selected="true"]::after { background: var(--accent); }
nav.tabs button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

.panel { display: none; }
.panel.active { display: block; }

h2 {
  font-family: "Instrument Serif", Georgia, serif; font-weight: 400;
  font-size: 24px; margin: 40px 0 6px;
}
.panel > h2:first-child { margin-top: 4px; }
.note { color: var(--muted); font-size: 14px; margin: 0 0 20px; max-width: 66ch; }

/* -- overview: filter + show list --------------------------------------- */
.filterrow { display: flex; gap: 10px; align-items: center; margin: 0 0 16px; }
.filterrow input {
  font: inherit; font-size: 14px; padding: 9px 12px; flex: 1; max-width: 320px;
  background: var(--surface); color: var(--ink); border: 1px solid var(--rule);
  border-radius: 8px;
}
.filterrow input:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.filterrow .count { font-size: 13px; color: var(--muted); white-space: nowrap; }

.shows { display: flex; flex-direction: column; gap: 12px; }
.show {
  background: var(--surface); border: 1px solid var(--rule); border-radius: 10px;
  padding: 16px 20px; box-shadow: var(--shadow);
  display: grid; grid-template-columns: minmax(180px, 1.1fr) auto 1fr; gap: 20px;
  align-items: center;
}
.show .name { font-size: 16px; font-weight: 600; line-height: 1.25; }
.show .venue { color: var(--muted); font-size: 13px; margin-top: 2px; }
.ladder {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-variant-numeric: tabular-nums; font-size: 15px; white-space: nowrap;
}
.ladder .to { color: var(--muted); padding: 0 4px; }
.ladder .lo { color: var(--cheap); font-weight: 600; }
.ladder .none { color: var(--muted); font-style: italic; font-size: 13px; }
.best { font-size: 12px; color: var(--muted); margin-top: 3px; white-space: nowrap; }
.best b { color: var(--ink); font-weight: 600; }
.strip { min-width: 0; }
.strip svg { display: block; width: 100%; height: 50px; overflow: visible; }

.tablewrap { overflow-x: auto; border: 1px solid var(--rule); border-radius: 10px;
             background: var(--surface); box-shadow: var(--shadow); }
table { border-collapse: collapse; width: 100%; font-size: 14px; }
th, td { padding: 10px 14px; text-align: left; border-bottom: 1px solid var(--rule); }
th {
  font-size: 11px; letter-spacing: .1em; text-transform: uppercase;
  color: var(--muted); font-weight: 600; white-space: nowrap;
}
tr:last-child td { border-bottom: none; }
td.num {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-variant-numeric: tabular-nums; text-align: right; white-space: nowrap;
}
.pill {
  display: inline-block; padding: 2px 9px; border-radius: 999px;
  font-size: 11px; letter-spacing: .03em; border: 1px solid var(--rule);
  color: var(--muted); white-space: nowrap;
}
.pill.good { color: var(--cheap); border-color: color-mix(in srgb, var(--cheap) 40%, transparent); }
.pill.warn { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 45%, transparent); }
.pill.low  { color: var(--dear);  border-color: color-mix(in srgb, var(--dear) 40%, transparent); }

/* -- by-show detail ------------------------------------------------------ */
.picker { display: flex; flex-wrap: wrap; gap: 12px 20px; align-items: flex-end; margin: 4px 0 26px; }
.picker label {
  display: block; font-size: 11px; letter-spacing: .1em; text-transform: uppercase;
  color: var(--muted); margin-bottom: 6px;
}
.picker select {
  font: inherit; font-size: 15px; padding: 10px 36px 10px 14px; min-width: 320px;
  background: var(--surface) url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="12" height="8"><path d="M1 1l5 5 5-5" stroke="%235C6B6A" stroke-width="1.6" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>') no-repeat right 14px center;
  color: var(--ink); border: 1px solid var(--rule); border-radius: 8px;
  appearance: none; cursor: pointer;
}
.picker select:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }

#detailCard { display: none; }
#detailCard.active { display: block; }
.detailhead {
  display: flex; justify-content: space-between; align-items: flex-start;
  flex-wrap: wrap; gap: 14px 24px; margin-bottom: 4px;
}
.detailhead h3 {
  font-family: "Instrument Serif", Georgia, serif; font-weight: 400;
  font-size: 28px; margin: 0; text-wrap: balance;
}
.detailhead .venue { color: var(--muted); font-size: 14px; margin-top: 2px; }
.detailladder { text-align: right; }
.detailladder .ladder { font-size: 20px; }
.detailladder .k { font-size: 11px; letter-spacing: .1em; text-transform: uppercase;
                    color: var(--muted); margin-top: 2px; }

.chartcard {
  background: var(--surface); border: 1px solid var(--rule); border-radius: 12px;
  padding: 18px 20px 12px; margin: 22px 0; box-shadow: var(--shadow);
}
.chartcard .chartlabel {
  font-size: 12px; letter-spacing: .08em; text-transform: uppercase;
  color: var(--muted); margin: 0 0 10px;
}
.chartwrap { position: relative; }
.chartwrap svg { display: block; width: 100%; height: 200px; overflow: visible; }
.chartwrap .grid line { stroke: var(--rule); stroke-width: 1; }
.chartwrap .axislabel { font: 11px "IBM Plex Mono", ui-monospace, monospace; fill: var(--muted); }
.chartwrap .trendline { fill: none; stroke: var(--accent); stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
.chartwrap .trenddot { fill: var(--accent); }
.chartwrap .trenddot.end { fill: var(--surface); stroke: var(--accent); stroke-width: 2.5; }
.tooltip {
  position: absolute; pointer-events: none; opacity: 0; transition: opacity .1s;
  background: var(--ink); color: var(--ground); font-size: 12px;
  padding: 6px 10px; border-radius: 6px; white-space: nowrap; transform: translate(-50%, -130%);
  font-family: "IBM Plex Mono", ui-monospace, monospace;
}
:root[data-theme="dark"] .tooltip,
:root:not([data-theme="light"]) .tooltip { color: var(--ground); }
@media (prefers-color-scheme: dark) { .tooltip { color: #0E1615; } }
:root[data-theme="dark"] .tooltip { color: #0E1615; }
.tooltip.show { opacity: 1; }
.chartempty { color: var(--muted); font-size: 13px; padding: 30px 4px; text-align: center; }

.legend { display: flex; gap: 18px; flex-wrap: wrap; align-items: center;
          font-size: 12px; color: var(--muted); margin: 0 0 14px; }
.sw { display: inline-block; width: 11px; height: 11px; border-radius: 2px;
      vertical-align: -1px; margin-right: 6px; }

footer {
  margin-top: 52px; padding-top: 18px; border-top: 1px solid var(--rule);
  color: var(--muted); font-size: 13px; max-width: 70ch;
}
"""

JS = """
(function () {
  var tabs = document.querySelectorAll('nav.tabs button');
  var panels = { overview: document.getElementById('tab-overview'),
                 byshow: document.getElementById('tab-byshow') };
  tabs.forEach(function (btn) {
    btn.addEventListener('click', function () {
      tabs.forEach(function (b) { b.setAttribute('aria-selected', 'false'); });
      Object.values(panels).forEach(function (p) { p.classList.remove('active'); });
      btn.setAttribute('aria-selected', 'true');
      panels[btn.dataset.tab].classList.add('active');
      if (btn.dataset.tab === 'byshow') { history.replaceState(null, '', '#by-show'); }
      else { history.replaceState(null, '', '#overview'); }
    });
  });
  if (location.hash === '#by-show') {
    document.querySelector('nav.tabs button[data-tab="byshow"]').click();
  }

  var filterInput = document.getElementById('showFilter');
  var showCards = document.querySelectorAll('.show[data-search]');
  var countEl = document.getElementById('showCount');
  function applyFilter() {
    var q = (filterInput.value || '').toLowerCase().trim();
    var shown = 0;
    showCards.forEach(function (el) {
      var hit = !q || el.dataset.search.indexOf(q) !== -1;
      el.style.display = hit ? '' : 'none';
      if (hit) shown++;
    });
    countEl.textContent = shown + ' of ' + showCards.length;
  }
  filterInput.addEventListener('input', applyFilter);
  applyFilter();

  var DATA = window.__SHOWDATA__;
  var select = document.getElementById('showSelect');
  var card = document.getElementById('detailCard');

  function money(v) {
    if (v === null || v === undefined) return '\\u2014';
    var s = v.toFixed(2).replace(/\\.00$/, '');
    return '\\u00a3' + s;
  }
  function bandClass(band) {
    band = (band || '').toLowerCase();
    if (band.indexOf('good') === 0 || band === 'on sale') return 'good';
    if (band.indexOf('medium') === 0) return 'warn';
    if (band.indexOf('low') === 0 || band.indexOf('limited') === 0) return 'low';
    return '';
  }
  function fmtDate(iso) {
    var d = new Date(iso);
    if (isNaN(d)) return iso;
    return d.toLocaleDateString('en-GB', { weekday: 'short', day: '2-digit', month: 'short', year: 'numeric' });
  }
  function fmtDateShort(iso) {
    var d = new Date(iso + 'T00:00:00');
    if (isNaN(d)) return iso;
    return d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' });
  }

  function renderTrend(container, trend) {
    container.innerHTML = '';
    var points = (trend || []).filter(function (p) { return p[1] !== null; });
    if (points.length === 0) {
      var e = document.createElement('div');
      e.className = 'chartempty';
      e.textContent = 'No price history yet for this show.';
      container.appendChild(e);
      return;
    }
    if (points.length === 1) {
      var e2 = document.createElement('div');
      e2.className = 'chartempty';
      e2.textContent = 'Only one day collected so far (' + fmtDateShort(points[0][0]) +
        ', ' + money(points[0][1]) + '). Check back tomorrow to see it move.';
      container.appendChild(e2);
      return;
    }

    var W = 900, H = 200, padL = 48, padR = 14, padT = 16, padB = 26;
    var prices = points.map(function (p) { return p[1]; });
    var lo = Math.min.apply(null, prices), hi = Math.max.apply(null, prices);
    if (lo === hi) { lo -= 1; hi += 1; }
    var span = hi - lo;
    var x = function (i) { return padL + (i / (points.length - 1)) * (W - padL - padR); };
    var y = function (v) { return H - padB - ((v - lo) / span) * (H - padT - padB); };

    var svgNS = 'http://www.w3.org/2000/svg';
    var svg = document.createElementNS(svgNS, 'svg');
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    svg.setAttribute('preserveAspectRatio', 'none');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', 'Cheapest price found on each day collected');

    var gridder = document.createElementNS(svgNS, 'g');
    gridder.setAttribute('class', 'grid');
    [lo, lo + span / 2, hi].forEach(function (v) {
      var line = document.createElementNS(svgNS, 'line');
      line.setAttribute('x1', padL); line.setAttribute('x2', W - padR);
      line.setAttribute('y1', y(v)); line.setAttribute('y2', y(v));
      gridder.appendChild(line);
      var lbl = document.createElementNS(svgNS, 'text');
      lbl.setAttribute('class', 'axislabel');
      lbl.setAttribute('x', 4); lbl.setAttribute('y', y(v) + 4);
      lbl.textContent = money(v);
      gridder.appendChild(lbl);
    });
    svg.appendChild(gridder);

    var d = points.map(function (p, i) { return (i === 0 ? 'M' : 'L') + x(i).toFixed(1) + ',' + y(p[1]).toFixed(1); }).join(' ');
    var path = document.createElementNS(svgNS, 'path');
    path.setAttribute('class', 'trendline');
    path.setAttribute('d', d);
    svg.appendChild(path);

    points.forEach(function (p, i) {
      var last = i === points.length - 1;
      var c = document.createElementNS(svgNS, 'circle');
      c.setAttribute('class', 'trenddot' + (last ? ' end' : ''));
      c.setAttribute('cx', x(i)); c.setAttribute('cy', y(p[1]));
      c.setAttribute('r', last ? 5 : 3.5);
      svg.appendChild(c);
    });

    // x-axis: first, middle-ish, last date
    [0, Math.floor((points.length - 1) / 2), points.length - 1].forEach(function (i, k) {
      if (k === 1 && points.length < 5) return;
      var lbl = document.createElementNS(svgNS, 'text');
      lbl.setAttribute('class', 'axislabel');
      lbl.setAttribute('x', x(i)); lbl.setAttribute('y', H - 6);
      lbl.setAttribute('text-anchor', i === 0 ? 'start' : (i === points.length - 1 ? 'end' : 'middle'));
      lbl.textContent = fmtDateShort(points[i][0]);
      svg.appendChild(lbl);
    });

    container.appendChild(svg);

    var tip = document.createElement('div');
    tip.className = 'tooltip';
    container.appendChild(tip);

    var hitLayer = document.createElementNS(svgNS, 'rect');
    hitLayer.setAttribute('x', 0); hitLayer.setAttribute('y', 0);
    hitLayer.setAttribute('width', W); hitLayer.setAttribute('height', H);
    hitLayer.setAttribute('fill', 'transparent');
    svg.appendChild(hitLayer);

    svg.addEventListener('mousemove', function (evt) {
      var rect = svg.getBoundingClientRect();
      var mx = (evt.clientX - rect.left) / rect.width * W;
      var i = Math.round((mx - padL) / (W - padL - padR) * (points.length - 1));
      i = Math.max(0, Math.min(points.length - 1, i));
      var px = x(i) / W * rect.width, py = y(points[i][1]) / H * rect.height;
      tip.style.left = px + 'px'; tip.style.top = py + 'px';
      tip.textContent = fmtDateShort(points[i][0]) + '  ' + money(points[i][1]);
      tip.classList.add('show');
    });
    svg.addEventListener('mouseleave', function () { tip.classList.remove('show'); });
  }

  function renderPerfs(tbody, perfs) {
    tbody.innerHTML = '';
    (perfs || []).forEach(function (p) {
      var tr = document.createElement('tr');
      var band = p[2];
      tr.innerHTML =
        '<td>' + fmtDate(p[0]) + '</td>' +
        '<td>' + (band ? '<span class="pill ' + bandClass(band) + '">' + band + '</span>' : '\\u2014') + '</td>' +
        '<td class="num">' + money(p[1]) + '</td>';
      tbody.appendChild(tr);
    });
  }

  function renderShow(key) {
    var s = DATA[key];
    if (!s) return;
    document.getElementById('dTitle').textContent = s.title;
    document.getElementById('dVenue').textContent = s.venue;
    document.getElementById('dLadder').innerHTML = s.lo === null
      ? '<span class="none">no price data (dates &amp; availability only)</span>'
      : '<span class="lo">' + money(s.lo) + '</span><span class="to">to</span>' + money(s.hi);
    document.getElementById('dPerfCount').textContent = s.perfs.length + ' performance' + (s.perfs.length === 1 ? '' : 's') + ' on sale';
    renderTrend(document.getElementById('dTrend'), s.trend);
    renderPerfs(document.getElementById('dPerfBody'), s.perfs);
    card.classList.add('active');
  }

  select.addEventListener('change', function () { renderShow(select.value); });
  if (select.value) { renderShow(select.value); }
})();
"""


def _money(v) -> str:
    return f"£{v:,.2f}".replace(".00", "")


def _weeks(rows):
    """Group performances into weeks, keeping the cheapest price in each."""
    by = defaultdict(list)
    for starts_at, price in rows:
        try:
            d = datetime.fromisoformat(starts_at).date()
        except (ValueError, TypeError):
            continue
        if price is None:
            continue
        iso = d.isocalendar()
        by[(iso[0], iso[1])].append((d, price))
    out = []
    for key in sorted(by):
        pairs = by[key]
        cheapest = min(p for _, p in pairs)
        out.append((min(d for d, _ in pairs), cheapest))
    return out


def _strip_svg(weeks) -> str:
    """A bar per week across the run, tallest where it is dearest.

    Single-hue emphasis: every bar is the accent colour, the cheapest week
    full-strength and the rest faded. Two shades of one hue, not two hues,
    so there is no colour pair for a colour-vision-deficient reader to
    confuse — see the CSS comment above for why that replaced the earlier
    accent/green pairing.
    """
    if not weeks:
        return ""
    lo = min(p for _, p in weeks)
    hi = max(p for _, p in weeks)
    span = (hi - lo) or 1.0
    n = len(weeks)
    W, H = 100.0, 40.0
    bw = W / n
    best_i = min(range(n), key=lambda i: weeks[i][1])

    parts = [f'<svg viewBox="0 0 {W:.0f} {H+12:.0f}" preserveAspectRatio="none" '
             f'role="img" aria-label="Cheapest ticket by week across the run">']
    parts.append(f'<line x1="0" y1="{H:.1f}" x2="{W:.0f}" y2="{H:.1f}" '
                 f'stroke="var(--rule)" stroke-width="0.6" vector-effect="non-scaling-stroke"/>')
    for i, (d, price) in enumerate(weeks):
        frac = (price - lo) / span
        h = 4 + frac * (H - 6)
        x = i * bw
        op = "1" if i == best_i else "0.3"
        parts.append(
            f'<rect x="{x + bw*0.14:.2f}" y="{H-h:.2f}" width="{bw*0.72:.2f}" height="{h:.2f}" '
            f'fill="var(--accent)" opacity="{op}" rx="0.4"><title>{d.strftime("%d %b %Y")} — '
            f'from {_money(price)}</title></rect>')
    parts.append("</svg>")
    return "".join(parts)


def build_html(conn: sqlite3.Connection) -> str:
    conn.row_factory = sqlite3.Row

    days = [r["d"] for r in conn.execute(
        "SELECT DISTINCT date(observed_at) d FROM price_observation ORDER BY d")]
    totals = conn.execute("""
        SELECT (SELECT COUNT(*) FROM show) shows,
               (SELECT COUNT(*) FROM performance) perfs,
               (SELECT COUNT(*) FROM price_observation) obs,
               (SELECT MIN(starts_at) FROM performance) lo,
               (SELECT MAX(starts_at) FROM performance) hi""").fetchone()

    shows = conn.execute("""
        SELECT s.key, s.title, s.venue,
               COUNT(DISTINCT p.id) perfs,
               MIN(o.min_price) lo, MAX(o.min_price) hi
        FROM show s
        JOIN performance p ON p.show_key = s.key
        LEFT JOIN price_observation o ON o.performance_id = p.id AND o.min_price IS NOT NULL
        GROUP BY s.key ORDER BY s.title""").fetchall()

    # latest reading per performance, for both the overview strips and the
    # by-show performance tables.
    perf_rows = defaultdict(list)
    for r in conn.execute("""
        SELECT p.show_key, p.starts_at, o.min_price, o.availability_band
        FROM performance p
        LEFT JOIN price_observation o
          ON o.performance_id = p.id
         AND o.observed_at = (SELECT MAX(observed_at) FROM price_observation o2
                              WHERE o2.performance_id = p.id)
        ORDER BY p.starts_at"""):
        perf_rows[r["show_key"]].append((r["starts_at"], r["min_price"], r["availability_band"]))

    # per-show, per-day cheapest price — the actual time series.
    trend_rows = defaultdict(list)
    for r in conn.execute("""
        SELECT p.show_key, date(o.observed_at) day, MIN(o.min_price) price
        FROM price_observation o
        JOIN performance p ON p.id = o.performance_id
        WHERE o.min_price IS NOT NULL
        GROUP BY p.show_key, day
        ORDER BY day"""):
        trend_rows[r["show_key"]].append([r["day"], r["price"]])

    def esc(x):
        return html.escape(str(x or ""))

    span = ""
    if totals["lo"] and totals["hi"]:
        a = datetime.fromisoformat(totals["lo"]).strftime("%d %b %Y")
        b = datetime.fromisoformat(totals["hi"]).strftime("%d %b %Y")
        span = f"{a} – {b}"

    # ---- build the embedded per-show JSON for the "By show" tab -----------
    showdata = {}
    for s in shows:
        rows = [r for r in perf_rows.get(s["key"], []) if r[0]]
        showdata[s["key"]] = {
            "title": s["title"],
            "venue": s["venue"] or "",
            "lo": s["lo"],
            "hi": s["hi"],
            "trend": trend_rows.get(s["key"], []),
            "perfs": [[r[0], r[1], r[2]] for r in rows],
        }

    o = []
    o.append('<title>West End Price Watch</title>')
    o.append('<link rel="preconnect" href="https://fonts.googleapis.com">')
    o.append('<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>')
    o.append('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
             'family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@400;600&'
             'family=Instrument+Serif&display=swap">')
    o.append(f"<style>{CSS}</style>")
    o.append('<div class="wrap">')

    o.append("<header>")
    o.append('<p class="eyebrow">London Theatre Geek · ticket price tracker</p>')
    o.append("<h1>West End Price Watch</h1>")
    o.append(f'<p class="sub">Cheapest advertised ticket for every performance on sale, '
             f'read from the box office once a day. Covering {span}.</p>')
    o.append("</header>")

    o.append('<div class="stats">')
    for n, k in ((totals["shows"], "shows tracked"),
                 (f'{totals["perfs"]:,}', "performances"),
                 (f'{totals["obs"]:,}', "price readings"),
                 (len(days), "day" + ("s" if len(days) != 1 else "") + " collected")):
        o.append(f'<div class="stat"><div class="n">{n}</div><div class="k">{k}</div></div>')
    o.append("</div>")

    o.append('<nav class="tabs" role="tablist">')
    o.append('<button type="button" data-tab="overview" aria-selected="true" role="tab">Overview</button>')
    o.append('<button type="button" data-tab="byshow" aria-selected="false" role="tab">By show</button>')
    o.append('</nav>')

    # ==================== OVERVIEW PANEL ====================
    o.append('<div class="panel active" id="tab-overview">')

    if len(days) < 2:
        o.append('<p class="note"><b>One day collected so far.</b> Price movement needs at '
                 'least two. Run the collector again tomorrow and this page will start '
                 'showing how prices change.</p>')

    o.append("<h2>Every show, cheapest to dearest</h2>")
    o.append('<p class="note">The bars show the cheapest seat available in each week of the '
             'run — taller means dearer. The solid bar is the cheapest week to go.</p>')
    o.append('<p class="legend">'
             '<span><span class="sw" style="background:var(--accent)"></span>cheapest week</span>'
             '<span><span class="sw" style="background:var(--accent);opacity:.3"></span>other weeks</span>'
             '</p>')

    o.append('<div class="filterrow">'
             '<input id="showFilter" type="text" placeholder="Filter by show or venue…" '
             'autocomplete="off" spellcheck="false">'
             '<span class="count" id="showCount"></span></div>')

    o.append('<div class="shows">')
    for s in shows:
        rows = [r for r in perf_rows.get(s["key"], []) if r[1] is not None]
        weeks = _weeks([(a, b) for a, b, _ in rows])
        best = min(rows, key=lambda r: r[1]) if rows else None
        search = f"{s['title']} {s['venue'] or ''}".lower()
        o.append(f'<div class="show" data-search="{esc(search)}">')
        o.append(f'<div><div class="name">{esc(s["title"])}</div>'
                 f'<div class="venue">{esc(s["venue"])}</div></div>')
        o.append('<div>')
        if s["lo"] is not None:
            o.append(f'<div class="ladder"><span class="lo">{_money(s["lo"])}</span>'
                     f'<span class="to">to</span>{_money(s["hi"])}</div>')
        else:
            o.append('<div class="ladder"><span class="none">dates only</span></div>')
        if best:
            when = datetime.fromisoformat(best[0]).strftime("%a %d %b")
            o.append(f'<div class="best">cheapest: <b>{when}</b> · {s["perfs"]} perfs</div>')
        else:
            o.append(f'<div class="best">{s["perfs"]} perfs on sale</div>')
        o.append("</div>")
        o.append(f'<div class="strip">{_strip_svg(weeks)}</div>')
        o.append("</div>")
    o.append("</div>")

    o.append("<h2>Cheapest date to see each show</h2>")
    o.append('<p class="note">The single cheapest performance currently on sale, and what the '
             'box office says about how full it is.</p>')
    o.append('<div class="tablewrap"><table>')
    o.append("<thead><tr><th>Show</th><th>Cheapest performance</th>"
             "<th>Availability</th><th style='text-align:right'>From</th>"
             "<th style='text-align:right'>Dearest seen</th></tr></thead><tbody>")
    for s in shows:
        rows = [r for r in perf_rows.get(s["key"], []) if r[1] is not None]
        if not rows:
            continue
        starts, price, band = min(rows, key=lambda r: r[1])
        when = datetime.fromisoformat(starts).strftime("%a %d %b %Y, %H:%M")
        cls = "good" if (band or "").lower().startswith("good") else (
              "low" if (band or "").lower().startswith(("low", "limited")) else "")
        pill = f'<span class="pill {cls}">{esc(band or "—")}</span>'
        o.append(f"<tr><td>{esc(s['title'])}</td><td>{when}</td><td>{pill}</td>"
                 f"<td class='num'>{_money(price)}</td>"
                 f"<td class='num'>{_money(s['hi'])}</td></tr>")
    o.append("</tbody></table></div>")
    o.append("</div>")  # /panel overview

    # ==================== BY-SHOW PANEL ====================
    o.append('<div class="panel" id="tab-byshow">')
    o.append("<h2>Look up one show</h2>")
    o.append('<p class="note">Pick a show to see its price history day by day, and every '
             'performance currently on sale.</p>')
    o.append('<div class="picker"><div><label for="showSelect">Show</label>'
             '<select id="showSelect">')
    for s in shows:
        o.append(f'<option value="{esc(s["key"])}">{esc(s["title"])} — {esc(s["venue"])}</option>')
    o.append('</select></div></div>')

    o.append('<div id="detailCard" class="active">')
    o.append('<div class="detailhead">')
    o.append('<div><h3 id="dTitle"></h3><div class="venue" id="dVenue"></div></div>')
    o.append('<div class="detailladder"><div class="ladder" id="dLadder"></div>'
             '<div class="k" id="dPerfCount"></div></div>')
    o.append('</div>')

    o.append('<div class="chartcard">')
    o.append('<p class="chartlabel">Cheapest price found, by day collected</p>')
    o.append('<div class="chartwrap" id="dTrend"></div>')
    o.append('</div>')

    o.append('<div class="tablewrap"><table>')
    o.append('<thead><tr><th>Performance</th><th>Availability</th>'
             "<th style='text-align:right'>Latest price</th></tr></thead>")
    o.append('<tbody id="dPerfBody"></tbody>')
    o.append('</table></div>')
    o.append('</div>')  # /detailCard
    o.append("</div>")  # /panel byshow

    o.append("<footer>")
    o.append("<p><b>How to read this.</b> Prices are the cheapest seat the box office "
             "advertises for that performance — the &ldquo;from&rdquo; price. They are not "
             "what every seat costs, and they move.</p>")
    o.append("<p>Where a performance shows limited availability, that means seats were "
             "unavailable when we looked. It does not necessarily mean they sold — houses "
             "hold seats back for the production, for comps, and for access bookings. Some "
             "venues (marked &ldquo;dates only&rdquo;) do not publish prices through the "
             "channel we read, so only dates and availability are shown.</p>")
    o.append(f"<p>Generated {date.today().strftime('%d %B %Y')} from "
             f"{totals['obs']:,} readings.</p>")
    o.append("</footer>")
    o.append("</div>")  # /wrap

    o.append(f'<script>window.__SHOWDATA__ = {json.dumps(showdata)};</script>')
    o.append(f"<script>{JS}</script>")

    return "\n".join(o)


def write_html(conn, path: str) -> str:
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(build_html(conn), encoding="utf-8")
    return str(p.resolve())

"""Turn the collected database into something you can actually look at.

Writes one self-contained HTML file — no internet needed to open it, no
libraries, nothing to install. Regenerate it after each collection run.

The page answers the question the whole project exists to answer:
"when is it cheapest to see this show, and is that changing?"
"""

import html
import sqlite3
from collections import defaultdict
from datetime import date, datetime

# --- palette -----------------------------------------------------------------
# Neutrals biased slightly teal so they read as chosen, not defaulted.
CSS = """
:root {
  --ground:  #F4F7F6;
  --surface: #FFFFFF;
  --ink:     #14201F;
  --muted:   #5C6B6A;
  --rule:    #DBE4E2;
  --accent:  #1F6F6B;
  --cheap:   #2E7D5B;
  --dear:    #B4472E;
  --shadow:  0 1px 2px rgba(20,32,31,.06), 0 8px 24px rgba(20,32,31,.05);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground:  #0E1615;
    --surface: #16211F;
    --ink:     #E8EFEE;
    --muted:   #93A5A3;
    --rule:    #26332F;
    --accent:  #4FB3A5;
    --cheap:   #5FBF8E;
    --dear:    #E08167;
    --shadow:  0 1px 2px rgba(0,0,0,.4), 0 8px 24px rgba(0,0,0,.3);
  }
}
:root[data-theme="dark"] {
  --ground:  #0E1615;
  --surface: #16211F;
  --ink:     #E8EFEE;
  --muted:   #93A5A3;
  --rule:    #26332F;
  --accent:  #4FB3A5;
  --cheap:   #5FBF8E;
  --dear:    #E08167;
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
.wrap { max-width: 1080px; margin: 0 auto; padding: 48px 24px 80px; }

header { border-bottom: 2px solid var(--ink); padding-bottom: 20px; margin-bottom: 32px; }
.eyebrow {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 11px; letter-spacing: .16em; text-transform: uppercase;
  color: var(--accent); margin: 0 0 10px;
}
h1 {
  font-family: "Instrument Serif", Georgia, "Times New Roman", serif;
  font-weight: 400; font-size: clamp(34px, 5vw, 52px); line-height: 1.05;
  margin: 0 0 8px; text-wrap: balance; letter-spacing: -.01em;
}
.sub { color: var(--muted); margin: 0; max-width: 62ch; }

.stats { display: flex; flex-wrap: wrap; gap: 28px 44px; margin: 28px 0 44px; }
.stat .n {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 30px; font-variant-numeric: tabular-nums; line-height: 1.1;
}
.stat .k {
  font-size: 11px; letter-spacing: .12em; text-transform: uppercase;
  color: var(--muted); margin-top: 4px;
}

h2 {
  font-family: "Instrument Serif", Georgia, serif; font-weight: 400;
  font-size: 26px; margin: 48px 0 6px;
}
.note { color: var(--muted); font-size: 14px; margin: 0 0 22px; max-width: 66ch; }

.shows { display: flex; flex-direction: column; gap: 14px; }
.show {
  background: var(--surface); border: 1px solid var(--rule); border-radius: 10px;
  padding: 18px 20px; box-shadow: var(--shadow);
  display: grid; grid-template-columns: minmax(190px, 1.1fr) auto 1fr; gap: 20px;
  align-items: center;
}
.show .name { font-size: 17px; font-weight: 600; line-height: 1.25; }
.show .venue { color: var(--muted); font-size: 13px; margin-top: 2px; }
.ladder {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-variant-numeric: tabular-nums; font-size: 15px; white-space: nowrap;
}
.ladder .to { color: var(--muted); padding: 0 4px; }
.ladder .lo { color: var(--cheap); font-weight: 600; }
.best { font-size: 12px; color: var(--muted); margin-top: 3px; white-space: nowrap; }
.best b { color: var(--ink); font-weight: 600; }
.strip { min-width: 0; }
.strip svg { display: block; width: 100%; height: 54px; overflow: visible; }

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
  display: inline-block; padding: 2px 8px; border-radius: 999px;
  font-size: 11px; letter-spacing: .04em; border: 1px solid var(--rule);
  color: var(--muted); white-space: nowrap;
}
.pill.good { color: var(--cheap); border-color: color-mix(in srgb, var(--cheap) 40%, transparent); }
.pill.low  { color: var(--dear);  border-color: color-mix(in srgb, var(--dear) 40%, transparent); }

footer {
  margin-top: 56px; padding-top: 18px; border-top: 1px solid var(--rule);
  color: var(--muted); font-size: 13px; max-width: 70ch;
}
.legend { display: flex; gap: 18px; flex-wrap: wrap; align-items: center;
          font-size: 12px; color: var(--muted); margin: 0 0 14px; }
.sw { display: inline-block; width: 11px; height: 11px; border-radius: 2px;
      vertical-align: -1px; margin-right: 6px; }
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
    The cheapest week is marked, because that is the useful one."""
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
        fill = "var(--cheap)" if i == best_i else "var(--accent)"
        op = "1" if i == best_i else "0.32"
        parts.append(
            f'<rect x="{x + bw*0.14:.2f}" y="{H-h:.2f}" width="{bw*0.72:.2f}" height="{h:.2f}" '
            f'fill="{fill}" opacity="{op}" rx="0.4"><title>{d.strftime("%d %b %Y")} — '
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
        JOIN price_observation o ON o.performance_id = p.id
        WHERE o.min_price IS NOT NULL
        GROUP BY s.key ORDER BY MIN(o.min_price)""").fetchall()

    # latest reading per performance
    perf_rows = defaultdict(list)
    for r in conn.execute("""
        SELECT p.show_key, p.starts_at, o.min_price, o.availability_band
        FROM performance p JOIN price_observation o ON o.performance_id = p.id
        WHERE o.min_price IS NOT NULL
          AND o.observed_at = (SELECT MAX(observed_at) FROM price_observation o2
                               WHERE o2.performance_id = p.id)
        ORDER BY p.starts_at"""):
        perf_rows[r["show_key"]].append((r["starts_at"], r["min_price"], r["availability_band"]))

    def esc(x):
        return html.escape(str(x or ""))

    span = ""
    if totals["lo"] and totals["hi"]:
        a = datetime.fromisoformat(totals["lo"]).strftime("%d %b %Y")
        b = datetime.fromisoformat(totals["hi"]).strftime("%d %b %Y")
        span = f"{a} – {b}"

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

    if len(days) < 2:
        o.append('<p class="note"><b>One day collected so far.</b> Price movement needs at '
                 'least two. Run the collector again tomorrow and this page will start '
                 'showing how prices change.</p>')

    o.append("<h2>Every show, cheapest to dearest</h2>")
    o.append('<p class="note">The bars show the cheapest seat available in each week of the '
             'run — taller means dearer. The highlighted bar is the cheapest week to go.</p>')
    o.append('<p class="legend">'
             '<span><span class="sw" style="background:var(--cheap)"></span>cheapest week</span>'
             '<span><span class="sw" style="background:var(--accent);opacity:.32"></span>other weeks</span>'
             '</p>')

    o.append('<div class="shows">')
    for s in shows:
        rows = perf_rows.get(s["key"], [])
        weeks = _weeks([(a, b) for a, b, _ in rows])
        best = min(rows, key=lambda r: r[1]) if rows else None
        o.append('<div class="show">')
        o.append(f'<div><div class="name">{esc(s["title"])}</div>'
                 f'<div class="venue">{esc(s["venue"])}</div></div>')
        o.append('<div>')
        o.append(f'<div class="ladder"><span class="lo">{_money(s["lo"])}</span>'
                 f'<span class="to">to</span>{_money(s["hi"])}</div>')
        if best:
            when = datetime.fromisoformat(best[0]).strftime("%a %d %b")
            o.append(f'<div class="best">cheapest: <b>{when}</b> · {s["perfs"]} perfs</div>')
        o.append("</div>")
        o.append(f'<div class="strip">{_strip_svg(weeks)}</div>')
        o.append("</div>")
    o.append("</div>")

    # cheapest dates table
    o.append("<h2>Cheapest date to see each show</h2>")
    o.append('<p class="note">The single cheapest performance currently on sale, and what the '
             'box office says about how full it is.</p>')
    o.append('<div class="tablewrap"><table>')
    o.append("<thead><tr><th>Show</th><th>Cheapest performance</th>"
             "<th>Availability</th><th style='text-align:right'>From</th>"
             "<th style='text-align:right'>Dearest seen</th></tr></thead><tbody>")
    for s in shows:
        rows = perf_rows.get(s["key"], [])
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

    o.append("<footer>")
    o.append("<p><b>How to read this.</b> Prices are the cheapest seat the box office "
             "advertises for that performance — the &ldquo;from&rdquo; price. They are not "
             "what every seat costs, and they move.</p>")
    o.append("<p>Where a performance shows limited availability, that means seats were "
             "unavailable when we looked. It does not necessarily mean they sold — houses "
             "hold seats back for the production, for comps, and for access bookings.</p>")
    o.append(f"<p>Generated {date.today().strftime('%d %B %Y')} from "
             f"{totals['obs']:,} readings.</p>")
    o.append("</footer>")
    o.append("</div>")
    return "\n".join(o)


def write_html(conn, path: str) -> str:
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(build_html(conn), encoding="utf-8")
    return str(p.resolve())

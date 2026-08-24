# West End Ticket Data Collector

Builds a daily time series of West End ticket prices and, where you want it,
seat-by-seat availability. The point of the thing is the *history* — what a
performance cost on each day of its run, and how fast the house emptied. That
record cannot be reconstructed later, which is why it's worth starting now
even if you never sell anything.

---

## Why it works this way

The seat data is real and rich — a Wicked performance carries 2,328 seat nodes,
each labelled `Row A, Seat 13, Sold out`. But **it is not in the HTML the server
returns.** Fetching that page as a plain HTTP request gives you a 2,948-byte
empty shell. The page builds itself in JavaScript.

So this uses Playwright: a real browser engine with no AI attached. It loads
the page, waits for the seat map to actually appear, reads the DOM, and closes.
Two to four seconds per page, deterministic, and free to run. That is a very
different animal from an AI agent clicking around a browser, which is what made
this feel impossible.

The one rule that matters: **`wait_for` a selector that only exists once the
data has rendered.** Skip it and you'll silently archive thousands of empty
shells and not notice for a month.

---

## Setup on Windows

```powershell
cd westend-tracker
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

Then open `config/settings.yaml` and **put your real email address in
`contact_email`**. This is not box-ticking. An honest user-agent naming the
project and a contact makes you *less* likely to be blocked — ops teams kill
anonymous aggressive bots and generally leave polite identified ones alone.

---

## Running it

```powershell
# Phase 1 — cheap. Run this every day from now on.
python -m wet.cli calendars

# See what you've got
python -m wet.cli report

# Phase 2 — heavier. Per-seat maps, a few performances per show.
python -m wet.cli seats --limit 6

# When a selector breaks (it will, sites get redesigned)
python -m wet.cli probe "https://www.atgtickets.com/shows/wicked/apollo-victoria-theatre/tickets/SOME-GUID" --wait "g[aria-label*='Seat'] circle" --headed
```

`probe` is the tool you'll use most. It loads one page, tells you how many seat
nodes it can see, prints the first few labels with their fill colours, and lists
every price in the page text. When a parser goes quiet, run `probe` and you'll
usually see why in ten seconds.

### Daily schedule

Task Scheduler, once a day, some time between 2am and 5am:

```
Program:    C:\path\to\westend-tracker\.venv\Scripts\python.exe
Arguments:  -m wet.cli calendars
Start in:   C:\path\to\westend-tracker
```

---

## Adding shows

Edit `config/shows.yaml`. You need the operator's own identifiers:

**ATG** — from the booking URL
`atgtickets.com/shows/{atg_show_slug}/{atg_venue_slug}/calendar/2026-08-23`

**LW** — from the ticketing URL
`ticketing.lwtheatres.co.uk/event/{lw_event_id}/?date=2026-08`

Both operators cover a whole estate on one parser, so adding a show is a
three-line config change, not code. ATG gives you Apollo Victoria, Lyceum,
Savoy, Piccadilly, Phoenix, Harold Pinter, Duke of York's, Fortune, Ambassadors
and Playhouse. LW gives you the Cambridge, Palladium, Drury Lane, Adelphi,
Gillian Lynne and His Majesty's.

**Not yet covered:** Nimax, Delfont Mackintosh, the Ticketmaster-run houses,
the subsidised theatres (mostly Spektrix), and off-West-End. Each is a new file
in `wet/parsers/` implementing four methods — deliberately a small contract.

---

## What's in the database

SQLite at `data/westend.db`. No server to run.

| Table | What it holds |
|---|---|
| `show`, `performance` | The registry |
| `price_observation` | **The core asset.** One row per performance per day: from-price, and ATG's own availability rating. Append-only — never updated, because overwriting a price destroys the history |
| `seat_snapshot` | One row per seat-map capture: totals, price-band counts, estimated gross range |
| `seat_state` | One row per seat, **written only when its state changes**. A full daily snapshot of every house would be ~30m rows a year for very little extra signal |

A useful first query once you have a few weeks:

```sql
SELECT p.starts_at, o.observed_at, o.days_to_perf, o.min_price, o.availability_band
FROM price_observation o
JOIN performance p ON p.id = o.performance_id
WHERE p.show_key = 'wicked-apollo-victoria'
ORDER BY p.starts_at, o.observed_at;
```

That's the shape of everything interesting: how a single performance's price
moved as its date approached.

---

## Staying on the right side of things

Not legal advice — take proper advice before you charge anyone for this.

- **Public, unauthenticated pages only.** Never create an account, never log in,
  never click "I agree" on a site you collect from. Terms you actively accept
  are enforceable; terms nobody clicked are far weaker. This single rule keeps
  you outside the contract and outside the Computer Misuse Act.
- **Don't republish their pages.** Publish derived figures — indices, medians,
  pace, percentages. Safer, and worth more anyway.
- **Label estimates as estimates.** You cannot tell a sold seat from a house
  hold, a comp or a production block from outside. Saying "Show X grossed £Y"
  as fact, when it's inferred, invites a different kind of letter.
- **Stay slow.** The defaults here are 4–9 seconds between pages and one page at
  a time. The whole West End at calendar level is ~300 loads a day. Resist
  every temptation to speed that up; being invisible is the whole strategy.

---

## Tests

```powershell
python tests\test_core.py
```

Covers the date parsing, money extraction, seat-reference normalisation and the
delta-write logic — including a case that reproduces the real Wicked reading
(2,328 seats, 1,655 gone, 71.1%). The DOM parsing needs a live page, so that's
what `probe` is for.

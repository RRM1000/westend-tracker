# Archive of old price readings

`readings-<year>.csv.gz` holds the price readings of performances that started
more than 180 days before the nightly run moved them here, one file per year of
the performance. They left `data/westend.db` so that file stays small (GitHub
refuses files over 100 MB); nothing in them was dropped.

There are no files here until the first performances become 180 days old, which
is late February 2027. After that `python -m wet.cli archive` (run by the
nightly workflow) appends to them. The files are written the same way every
time, so a night with nothing to move changes no bytes.

## What a row is

One reading: a stretch of time over which a performance showed the same
price and availability. Columns:

| column | meaning |
|---|---|
| `show_key`, `external_id`, `starts_at` | the show and performance, by the operator's own keys (not this database's row ids) |
| `first_seen`, `last_seen` | first and last time we looked and saw exactly this (UTC) |
| `first_days_to_perf`, `last_days_to_perf` | days before the performance, at those two moments |
| `day_times` | seconds after midnight UTC (5 digits each, concatenated) of the last sighting on each day from `first_seen`'s date to the day before `last_seen`'s |
| `min_price`, `price_bands_json`, `availability_band`, `on_sale` | the reading itself; empty = NULL |
| `source_url` | the calendar page of the first sighting |

## Reading and restoring

```python
from wet import archive
for row in archive.read_archive("data/archive/readings-2026.csv.gz"):
    print(row["show_key"], row["starts_at"], row["first_seen"], row["min_price"])
```

or, from the shell, into a scratch database rather than the live one:

```
python -m wet.cli restore --year 2026 --database data/scratch.db
python -m wet.cli restore --show wicked-apollo-victoria --database data/scratch.db
```

`zcat data/archive/readings-2026.csv.gz | head` (or any CSV tool after
unzipping) works too. Restoring into `data/westend.db` itself is allowed, but
the next nightly run will move the same readings out again.

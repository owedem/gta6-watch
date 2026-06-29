# gta6-watch

An **official-source** early-warning + knowledge monitor for Grand Theft Auto VI.

It watches the plumbing of Rockstar's official surfaces — the GTA6 site, its JS
bundles, Newswire, Take-Two investor relations, robots/sitemap, and a set of
unlinked-page probes — diffs every run against the last snapshot, scores each
change by severity, and **alerts only on what matters** while keeping a living,
timestamped knowledge base. No rumor, no leaks, no noise.

> Why official-only works: Rockstar builds the page, uploads the assets, wires
> the route, and swaps the share image **before** they flip anything live.
> Watching the infrastructure puts you minutes-to-days ahead of the news.
> Full reasoning in **[MONITORING_SPEC.md](MONITORING_SPEC.md)**.

## What it tracks

- **Site pages** — title, meta description, OG/Twitter share image, `/VI/*` routes, media assets + counts, JS bundle URLs, deploy fingerprint.
- **JS bundles** — greps for hidden routes, date strings, price strings, API URLs, and watchlist keywords (`pc`, `steam`, `online`, `collector`, …).
- **Newswire** — new GTA VI articles (scored CRITICAL).
- **Take-Two IR / SEC** — pages that often confirm dates & money first.
- **Unlinked-page probes** — `/VI/pc`, `/VI/online`, `/VI/buy`, `/VI/collectors-edition`, … A 404→200 flip is a top signal.
- **robots.txt & sitemap** — new URLs appear here before they're linked.
- **Storefronts** (optional) — a live Steam/Epic page *is* the PC reveal.

## Files

```
monitor.py            the bot: scrape → diff → classify → alert → emit data
targets.yaml          all watch targets, keywords, and severity rules (tune here)
state/knowledge.json  structured official canon (the "be-complete" side)
state/snapshot.json   latest scraped state (auto)
state/changelog.json  append-only detected signals (auto)
docs/index.html       the live dashboard
docs/data.js          dashboard data (auto-written each run)
BASELINE.md           human-readable baseline of everything official today
MONITORING_SPEC.md    the doctrine: what we watch and why
.github/workflows/monitor.yml   runs the bot every 15 min on GitHub Actions
```

## Run it locally

```bash
pip install -r requirements.txt
python monitor.py --selftest   # offline test of the diff/classify engine
python monitor.py --init       # capture the first baseline (no alerts)
python monitor.py              # subsequent runs: diff, record, alert
python monitor.py --dry-run    # scrape + show what WOULD alert, change nothing
```

Open `docs/index.html` in a browser to view the dashboard at any time.

## Deploy on GitHub (recommended)

1. Push this folder to a new GitHub repo.
2. **Actions** is pre-wired (`.github/workflows/monitor.yml`) to run every 15 minutes and commit state back. No setup needed for GitHub-issue alerts — it uses the built-in token.
3. **Alerts**
   - *GitHub issues* — on by default; a CRITICAL/HIGH signal opens a labeled issue.
   - *Discord/Slack* — add a repo secret `ALERT_WEBHOOK` with your webhook URL to also get pushed messages.
4. **Live dashboard** — enable **Settings → Pages → Deploy from branch → `/docs`**. Your dashboard updates itself every run.

## Tuning

Everything you'd want to change lives in **`targets.yaml`**: add probe paths,
adjust the keyword watchlist, change which change-types map to which severity,
raise/lower the `alert_threshold`, or flip storefront watching on. As open
questions resolve (PC, GTA Online, Collector's edition, Trailer 3), prune
answered keywords and add probes for the next unknown.

---
*Official sources only. Built to be first, and to stay complete.*

# Monitoring Spec — what we watch and why

The whole system rests on one bet: **official channels leak the future through infrastructure before they publish content.** A page is built, assets are uploaded, a route is wired, and the share image is swapped *before* anyone flips the switch. We watch the plumbing, not the press release.

Everything below is configured in `targets.yaml`. This document explains the reasoning so the config stays principled as the site changes.

## The surfaces

**1. The GTA6 site (`rockstargames.com/VI` and subpaths).** The richest surface. On each watched page we capture the title and meta description, the Open Graph and Twitter share tags (especially the share *image* URL), every internal `/VI/*` route in the nav, the set of media asset URLs, the JS bundle URLs, and a deploy fingerprint. The media page additionally gets an item count. Most early signals live here.

**2. The JS bundles.** We fetch the linked JavaScript and grep it for things the rendered page doesn't show yet: hidden route strings, date strings, price strings, API endpoints, and keywords from the watchlist (`pc`, `steam`, `online`, `collector`, etc.). Rockstar's front end frequently references routes and features that aren't linked or live — the bundle is where "what's coming" is written down first.

**3. Rockstar Newswire.** The official announcement feed, filtered to the GTA VI tag. A new article is by definition news; it's scored CRITICAL.

**4. Take‑Two investor relations & SEC filings.** The money side often confirms hard facts — dates, financials, "available now" — in an 8‑K or earnings note before, or simultaneously with, the consumer site. Cheap to watch, occasionally first.

**5. Unlinked‑page probes.** We directly request URLs that don't exist yet but plausibly will: `/VI/pc`, `/VI/online`, `/VI/buy`, `/VI/collectors-edition`, `/VI/trailer-3`, and friends. A transition from 404 to 200 (or 301/302) is one of the strongest possible early signals — the page is live but not yet linked.

**6. robots.txt & sitemaps.** New URLs frequently appear in the sitemap (or get un‑blocked in robots.txt) before they're linked anywhere in the UI. We diff both.

**7. First‑party storefronts (optional toggle).** A Steam, Epic, or Rockstar Launcher product page going live for GTA6 *is* the PC announcement. Off by default to honor "official only" strictly; flip `storefronts.enabled: true` in the config to arm it.

## The signal model

Every scrape is diffed against the last snapshot (`state/snapshot.json`). Each difference is typed (e.g. `og_image_changed`, `new_route`, `new_probe_live`) and mapped to a severity in `targets.yaml`:

- **CRITICAL** — a new Newswire article, an unlinked page going live, or a critical keyword (`pc`, `steam`, `system requirements`…) appearing. This is "wake me up."
- **HIGH** — share image swapped, a new route, a new sitemap URL, title/description change, or a new date/price string. Strong pre‑announcement tells.
- **MEDIUM** — new media asset, media count change, robots.txt change, secondary keywords.
- **LOW** — JS bundle hash changed but content is unchanged. Usually a routine redeploy.
- **NOISE** — only the deploy fingerprint (ETag/Last‑Modified) moved. Silent by design.

Alerts fire at the `alert_threshold` (default **HIGH**) and above. LOW and NOISE are still recorded in the change log so the deploy cadence is visible on the dashboard — they just don't ping you. This is the anti‑spam contract: routine rebuilds never wake you, real movement always does.

## Why this beats reading the news

By the time a gaming outlet publishes, the signal already passed through the infrastructure we watch — the page existed, the asset was uploaded, the route resolved. Watching official plumbing puts us at the source, minutes to days ahead, with zero dependence on anyone else's reporting and zero noise from rumor.

## Tuning

The watchlist and severity map are intentionally in `targets.yaml`, not in code. As the launch approaches and the open questions (PC, GTA Online, Collector's edition, Trailer 3) resolve, prune answered keywords and add new probes for whatever becomes the next unknown.

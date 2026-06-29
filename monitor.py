#!/usr/bin/env python3
"""
gta6-watch — official-source early-warning + knowledge monitor.

Watches the official GTA6 surfaces (site, JS bundles, Newswire, Take-Two IR,
robots/sitemap, unlinked-page probes, and the media-download CDN), diffs against
the last snapshot, classifies every change by severity, records a changelog,
fires alerts at or above a threshold, and emits dashboard data.

Usage:
    python monitor.py            # run once: scrape, diff, record, alert
    python monitor.py --init     # capture baseline snapshot, no alerts
    python monitor.py --dry-run  # scrape + diff + print, but don't persist/alert
    python monitor.py --selftest # offline unit test of the diff/classify engine

Outputs (in state/ and docs/):
    state/snapshot.json   latest scraped state
    state/changelog.json  append-only list of detected changes
    state/status.json     last run metadata + probe/deploy status
    docs/data.js          window.GTA6_DATA = {...}  (read by the dashboard)

Alerts (when severity >= alert_threshold):
    - stdout (always)
    - GitHub issue   (if GITHUB_TOKEN + GITHUB_REPOSITORY are set)
    - webhook POST   (if ALERT_WEBHOOK is set; Discord/Slack compatible)

Only standard library + requests + beautifulsoup4 + pyyaml are required.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None
try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None
try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
DOCS = ROOT / "docs"
SEV_ORDER = ["NOISE", "LOW", "MEDIUM", "HIGH", "CRITICAL"]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "ignore")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config(path: Path) -> dict:
    if yaml is None:
        raise SystemExit("PyYAML is required: pip install pyyaml")
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def severity_lookup(cfg: dict) -> dict:
    """Invert the severity map: change_type -> severity name."""
    out = {}
    for sev, types in (cfg.get("severity") or {}).items():
        for t in types or []:
            out[t] = sev
    return out


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def http_get(url: str, cfg: dict):
    """Return (status, headers, text). status 0 on network error."""
    h = cfg.get("http", {})
    headers = {"User-Agent": h.get("user_agent", "gta6-watch/1.0"),
               "Accept-Language": h.get("accept_language", "en-US,en;q=0.9")}
    timeout = h.get("timeout_seconds", 20)
    retries = h.get("retries", 2)
    delay = h.get("politeness_delay_seconds", 2)
    last_exc = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
            time.sleep(delay)
            return r.status_code, dict(r.headers), r.text
        except Exception as exc:  # network hiccup -> retry
            last_exc = exc
            time.sleep(delay)
    return 0, {"error": str(last_exc)}, ""


def head_meta(url: str, cfg: dict) -> dict:
    """HEAD a URL (with ranged-GET fallback) to read size/etag/last-modified
    without downloading the body. Used for the media-CDN download bundles."""
    h = cfg.get("http", {})
    ua = h.get("user_agent", "gta6-watch/1.0")
    to = h.get("timeout_seconds", 20)
    try:
        r = requests.head(url, headers={"User-Agent": ua}, timeout=to, allow_redirects=True)
        if r.status_code in (403, 405, 501):  # HEAD not allowed -> tiny ranged GET
            r = requests.get(url, headers={"User-Agent": ua, "Range": "bytes=0-0"},
                             timeout=to, allow_redirects=True, stream=True)
            r.close()
        time.sleep(h.get("politeness_delay_seconds", 1))
        cr = r.headers.get("Content-Range")
        size = cr.split("/")[-1] if cr else r.headers.get("Content-Length")
        return {"status": r.status_code, "size": size,
                "last_modified": r.headers.get("Last-Modified"),
                "etag": r.headers.get("ETag")}
    except Exception as exc:
        return {"status": 0, "error": str(exc)}


# --------------------------------------------------------------------------- #
# Extraction (pure: html/headers -> features). Testable offline.
# --------------------------------------------------------------------------- #
def parse_page(html: str, headers: dict, extract: list, base="https://www.rockstargames.com") -> dict:
    feat: dict = {}
    soup = BeautifulSoup(html, "html.parser") if (BeautifulSoup and html) else None

    def meta(attr, val):
        if not soup:
            return None
        tag = soup.find("meta", attrs={attr: val})
        return tag.get("content") if tag and tag.get("content") else None

    if "title" in extract:
        feat["title"] = (soup.title.string.strip() if soup and soup.title and soup.title.string else None)
    if "meta" in extract:
        feat["description"] = meta("name", "description")
    if "og" in extract:
        feat["og_title"] = meta("property", "og:title")
        feat["og_image"] = meta("property", "og:image")
    if "twitter" in extract:
        feat["twitter_image"] = meta("name", "twitter:image")
    if "routes" in extract and soup:
        routes = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/VI" in href:
                routes.add(href.split("?")[0].split("#")[0])
        feat["routes"] = sorted(routes)
    if ("media" in extract or "media_count" in extract) and soup:
        imgs = set()
        for img in soup.find_all("img", src=True):
            src = img["src"]
            if any(k in src.lower() for k in ("media", "screenshot", "artwork", "/vi/", "gtavi")):
                imgs.add(src.split("?")[0])
        if "media" in extract:
            feat["media"] = sorted(imgs)
        if "media_count" in extract:
            feat["media_count"] = len(imgs)
    if "bundles" in extract and soup:
        bundles = set()
        for s in soup.find_all("script", src=True):
            src = s["src"]
            if src.endswith(".js") or "/_next/" in src or "chunk" in src:
                bundles.add(src if src.startswith("http") else base + src)
        feat["bundle_urls"] = sorted(bundles)
    if "media_counts" in extract:
        # Parse category counts like "Videos11", "Screenshots70" from the media page
        counts = {}
        for label in ("Videos", "Screenshots", "Ultimate Edition Benefits",
                      "Vintage Vice City Pack", "Artwork & Wallpapers"):
            m = re.search(re.escape(label) + r"\s*(\d{1,4})", html or "")
            if m:
                counts[label] = int(m.group(1))
        feat["media_counts"] = counts
    if "fingerprint" in extract:
        feat["fingerprint"] = {
            "etag": headers.get("ETag"),
            "last_modified": headers.get("Last-Modified"),
            "html_hash": sha(html),
        }
    if "newswire" in extract and soup:
        arts = []
        seen = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/newswire/article/" in href and href not in seen:
                seen.add(href)
                arts.append({"url": href if href.startswith("http") else base + href,
                             "title": a.get_text(strip=True)[:200]})
        feat["newswire"] = arts[:40]
    return feat


def grep_blob(text: str, cfg: dict) -> dict:
    """Find watchlist keywords, dates, prices, and API URLs in a text blob."""
    kw = cfg.get("keywords", {})
    low = (text or "").lower()
    found = {"critical": [], "high": [], "medium": []}
    for tier in ("critical", "high", "medium"):
        for term in kw.get(tier, []) or []:
            if str(term).lower() in low:
                found[tier].append(str(term))
    years = sorted(set(re.findall(r"\b20[2-3]\d\b", text or "")))
    prices = sorted(set(re.findall(kw.get("price_regex", r"\$\d{2,3}\.\d{2}"), text or "")))
    apis = sorted(set(re.findall(r"https?://[a-z0-9.\-]+/[a-z0-9/_\-]*api[a-z0-9/_\-]*", low)))[:40]
    return {"keywords": found, "years": years, "prices": prices, "api_urls": apis}


# --------------------------------------------------------------------------- #
# Certificate Transparency (subdomain early-warning)
# --------------------------------------------------------------------------- #
def scrape_ct(cfg: dict) -> dict:
    """Query CT logs (crt.sh) for subdomains of the watched domains.
    A new subdomain appears here when a cert is provisioned -- often before the
    subdomain is linked/announced. Wildcard certs (*.domain) reveal no specific
    name; if that's all we see, wildcard_only stays True (vector is limited)."""
    ct = cfg.get("cert_transparency", {})
    if not ct.get("enabled"):
        return {"subdomains": [], "note": "disabled"}
    subs = set()
    wildcard_only = True
    errors = []
    for domain in ct.get("domains", []) or []:
        url = f"https://crt.sh/?q=%25.{domain}&output=json"
        try:
            r = requests.get(url, headers={"User-Agent": cfg.get("http", {}).get("user_agent", "gta6-watch/1.0")},
                             timeout=cfg.get("http", {}).get("timeout_seconds", 20))
            for row in (r.json() or []):
                for name in (row.get("name_value", "") or "").split("\n"):
                    name = name.strip().lower()
                    if name.endswith(domain) and "@" not in name:
                        if name.startswith("*."):
                            continue
                        subs.add(name)
                        wildcard_only = False
        except Exception as exc:
            errors.append(f"crt.sh {domain}: {exc}")
        time.sleep(cfg.get("http", {}).get("politeness_delay_seconds", 2))
    return {"subdomains": sorted(subs), "wildcard_only": wildcard_only, "errors": errors}


def parse_manifest_routes(html: str, manifest_js: str, base: str) -> dict:
    """Pure: given the page HTML (to find the buildId) and the _buildManifest.js
    body, return the buildId + every route the app declares. New routes here are
    pages that are BUILT but may not be linked anywhere yet."""
    m = re.search(r"/_next/static/([^/\"']+)/_buildManifest\.js", html or "")
    if not m:
        m = re.search(r'"buildId"\s*:\s*"([^"]+)"', html or "")
    build_id = m.group(1) if m else None
    routes = set()
    for r in re.findall(r"""["'](/[A-Za-z0-9_\-/\[\]]*)["']""", manifest_js or ""):
        if not r.endswith(".js") and "static/" not in r:
            routes.add(r)
    return {"buildId": build_id, "routes": sorted(routes)}


def scrape_manifest_routes(cfg: dict) -> dict:
    """Fetch the home page, locate the Next.js buildId, pull _buildManifest.js,
    and enumerate every declared route (incl. unlinked ones)."""
    base = "https://www.rockstargames.com"
    pages = cfg.get("pages") or [{}]
    home = pages[0].get("url", base + "/VI")
    _, _, html = http_get(home, cfg)
    m = re.search(r"/_next/static/([^/\"']+)/_buildManifest\.js", html or "")
    if not m:
        m = re.search(r'"buildId"\s*:\s*"([^"]+)"', html or "")
    if not m:
        return {"buildId": None, "routes": [], "note": "buildId not found"}
    build_id = m.group(1)
    man_url = f"{base}/VI/_next/static/{build_id}/_buildManifest.js"
    _, _, js = http_get(man_url, cfg)
    return parse_manifest_routes(html, js, base)


def scrape_discovery(cfg: dict) -> dict:
    """Bounded crawl of official hub pages to discover NEW urls anywhere on the
    official site -- including pages/microsites OUTSIDE the GTA6 app route table,
    as long as they're linked from somewhere official. Discovers broadly; only
    GTA6-relevant new urls alert (see diff). Everything else just grows baseline."""
    from urllib.parse import urljoin, urlparse
    disc = cfg.get("discovery", {})
    if not disc.get("enabled"):
        return {"urls": [], "visited": 0}
    allow = [str(d).lower() for d in (disc.get("official_domains", []) or [])]
    max_pages = disc.get("max_pages", 25)
    max_depth = disc.get("max_depth", 1)

    def is_official(host):
        host = (host or "").lower()
        return any(host == d or host.endswith("." + d) for d in allow)

    found, visited = set(), set()
    queue = [(u, 0) for u in (disc.get("seeds", []) or [])]
    while queue and len(visited) < max_pages:
        url, depth = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        status, _, html = http_get(url, cfg)
        if status != 200 or not html or BeautifulSoup is None:
            continue
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            link = urljoin(url, a["href"]).split("#")[0].split("?")[0].rstrip("/")
            if not link.startswith("http"):
                continue
            if is_official(urlparse(link).netloc):
                found.add(link)
                if depth < max_depth and link not in visited:
                    queue.append((link, depth + 1))
    return {"urls": sorted(found), "visited": len(visited)}


# --------------------------------------------------------------------------- #
# Scrape (network)
# --------------------------------------------------------------------------- #
def scrape(cfg: dict) -> dict:
    snap = {"generated": now_iso(), "pages": {}, "bundles": {}, "bundle_findings": {},
            "probes": {}, "robots_hash": None, "sitemap_urls": [], "newswire": [],
            "investor": {}, "downloads": {}, "share_images": {}, "errors": []}

    # Pages
    for page in (cfg.get("pages") or []):
        status, headers, html = http_get(page["url"], cfg)
        if status != 200:
            snap["errors"].append(f"{page['id']}: HTTP {status}")
        snap["pages"][page["id"]] = parse_page(html, headers, page.get("extract", []))

    # Newswire feeds
    for nw in (cfg.get("newswire") or []):
        status, headers, html = http_get(nw["url"], cfg)
        feat = parse_page(html, headers, nw.get("extract", ["newswire"]))
        snap["newswire"].extend(feat.get("newswire", []))

    # Investor pages (lightweight: status + hash)
    for inv in (cfg.get("investor") or []):
        status, headers, html = http_get(inv["url"], cfg)
        snap["investor"][inv["id"]] = {"status": status, "hash": sha(html)}

    # JS bundles -> grep
    all_bundle_text = []
    bundle_urls = set()
    for pid, feat in snap["pages"].items():
        for b in feat.get("bundle_urls", []) or []:
            bundle_urls.add(b)
    for b in sorted(bundle_urls):
        status, headers, text = http_get(b, cfg)
        snap["bundles"][b] = sha(text)
        if text:
            all_bundle_text.append(text[:2_000_000])  # cap per bundle
    snap["bundle_findings"] = grep_blob("\n".join(all_bundle_text), cfg)

    # Probes
    probe = cfg.get("probe_paths", {})
    base = probe.get("base", "https://www.rockstargames.com")
    for p in (probe.get("paths") or []):
        status, _, _ = http_get(base + p, cfg)
        snap["probes"][p] = status

    # Storefronts (optional)
    sf = cfg.get("storefronts", {})
    if sf.get("enabled"):
        snap["storefronts"] = {}
        for u in sf.get("urls", []):
            status, _, _ = http_get(u, cfg)
            snap["storefronts"][u] = status

    # robots + sitemap
    rs = cfg.get("robots_sitemap", {})
    if rs.get("robots"):
        _, _, robots = http_get(rs["robots"], cfg)
        snap["robots_hash"] = sha(robots)
    sitemap_urls = set()
    for sm in (rs.get("sitemaps") or []):
        _, _, xml = http_get(sm, cfg)
        for m in re.findall(r"<loc>([^<]+)</loc>", xml or ""):
            if "/VI" in m or "gtavi" in m.lower():
                sitemap_urls.add(m.strip())
    snap["sitemap_urls"] = sorted(sitemap_urls)

    # Download bundles on the media CDN — HEAD only (the media tripwire)
    dl = cfg.get("downloads", {})
    for url in (dl.get("known", []) or []) + (dl.get("guessed", []) or []):
        snap["downloads"][url] = head_meta(url, cfg)

    # Content-hashed share images — swap is a pre-announcement tell
    for url in (cfg.get("share_images") or []):
        snap["share_images"][url] = head_meta(url, cfg)

    # Certificate Transparency — new subdomains leak here at provisioning
    snap["ct"] = scrape_ct(cfg)

    # Build manifest — every declared route, incl. pages built but not linked
    if cfg.get("manifest_routes", {}).get("enabled", True):
        snap["manifest_routes"] = scrape_manifest_routes(cfg)

    # Discovery crawl — NEW urls anywhere official (microsites outside the app)
    snap["discovery"] = scrape_discovery(cfg)

    return snap


# --------------------------------------------------------------------------- #
# Diff + classify
# --------------------------------------------------------------------------- #
def diff(old: dict, new: dict, cfg: dict) -> list:
    t2s = severity_lookup(cfg)
    changes = []

    def add(ctype, target, detail):
        changes.append({"type": ctype, "severity": t2s.get(ctype, "MEDIUM"),
                        "target": target, "detail": detail, "at": now_iso()})

    old = old or {}
    op = old.get("pages", {})
    for pid, nf in new.get("pages", {}).items():
        of = op.get(pid, {})
        if of.get("title") != nf.get("title") and nf.get("title"):
            add("title_changed", pid, f"{of.get('title')!r} -> {nf.get('title')!r}")
        if of.get("description") != nf.get("description") and nf.get("description"):
            add("desc_changed", pid, f"description changed on {pid}")
        if of.get("og_image") != nf.get("og_image") and nf.get("og_image"):
            add("og_image_changed", pid, f"share image -> {nf.get('og_image')}")
        for r in set(nf.get("routes", [])) - set(of.get("routes", [])):
            add("new_route", pid, f"new route: {r}")
        for m in set(nf.get("media", [])) - set(of.get("media", [])):
            add("new_media_asset", pid, f"new media: {m}")
        if "media_count" in nf and of.get("media_count") not in (None, nf.get("media_count")):
            add("media_count_changed", pid, f"media count {of.get('media_count')} -> {nf.get('media_count')}")
        # category media counts (Videos/Screenshots/...)
        ocounts = of.get("media_counts", {}) or {}
        for label, n in (nf.get("media_counts", {}) or {}).items():
            if label in ocounts and ocounts[label] != n:
                add("media_count_changed", pid, f"{label}: {ocounts[label]} -> {n}")

    # bundles
    for b, h in new.get("bundles", {}).items():
        if b in old.get("bundles", {}) and old["bundles"][b] != h:
            add("bundle_hash_changed", b, "JS bundle redeployed")

    # bundle findings: keywords / years / prices newly appearing
    of = old.get("bundle_findings", {})
    nf = new.get("bundle_findings", {})
    for tier, ctype in (("critical", "critical_keyword_new"),
                        ("high", "high_keyword_new"),
                        ("medium", "medium_keyword_new")):
        new_kw = set((nf.get("keywords", {}) or {}).get(tier, [])) - set((of.get("keywords", {}) or {}).get(tier, []))
        for k in sorted(new_kw):
            add(ctype, "bundle", f"keyword appeared: '{k}'")
    for y in set(nf.get("years", [])) - set(of.get("years", [])):
        add("date_string_new", "bundle", f"new date string: {y}")
    for pr in set(nf.get("prices", [])) - set(of.get("prices", [])):
        add("price_string_new", "bundle", f"new price string: {pr}")

    # probes: 404/none -> live
    op_pr = old.get("probes", {})
    for p, status in new.get("probes", {}).items():
        was = op_pr.get(p)
        live_now = status in (200, 301, 302)
        was_live = was in (200, 301, 302)
        if live_now and not was_live and was is not None:
            add("new_probe_live", p, f"unlinked page now live (HTTP {status})")
        elif live_now and was is None:
            add("new_probe_live", p, f"unlinked page live on first observation (HTTP {status})")

    # storefronts
    for u, status in (new.get("storefronts", {}) or {}).items():
        was = (old.get("storefronts", {}) or {}).get(u)
        if status in (200,) and was not in (200, None):
            add("storefront_live", u, f"storefront page live (HTTP {status})")

    # sitemap
    for u in set(new.get("sitemap_urls", [])) - set(old.get("sitemap_urls", [])):
        add("new_sitemap_url", "sitemap", f"new sitemap URL: {u}")

    # robots
    if old.get("robots_hash") and old.get("robots_hash") != new.get("robots_hash"):
        add("robots_changed", "robots.txt", "robots.txt changed")

    # newswire
    old_urls = {a["url"] for a in old.get("newswire", [])}
    for a in new.get("newswire", []):
        if a["url"] not in old_urls:
            add("new_newswire_article", "newswire", f"{a.get('title') or a['url']}")

    # download bundles: size/etag change => new media; 404->200 => new bundle
    old_dl = old.get("downloads", {}) or {}
    for url, meta in (new.get("downloads", {}) or {}).items():
        om = old_dl.get(url)
        live = meta.get("status") in (200, 206, 301, 302)
        if om is None:
            if live:
                add("new_download_live", url, f"media bundle present (HTTP {meta.get('status')})")
        else:
            was_live = om.get("status") in (200, 206, 301, 302)
            if live and not was_live:
                add("new_download_live", url, f"media bundle WENT LIVE (HTTP {meta.get('status')})")
            elif live and (om.get("size") != meta.get("size")
                           or om.get("etag") != meta.get("etag")
                           or om.get("last_modified") != meta.get("last_modified")):
                add("download_changed", url, f"media bundle updated (size {om.get('size')} -> {meta.get('size')})")

    # share images: etag / last-modified swap
    old_si = old.get("share_images", {}) or {}
    for url, meta in (new.get("share_images", {}) or {}).items():
        om = old_si.get(url)
        if om and (om.get("etag") != meta.get("etag")
                   or om.get("last_modified") != meta.get("last_modified")):
            add("share_image_changed", url, "share image swapped")

    # certificate transparency: new subdomains (hot tokens => CRITICAL)
    hot = [str(t).lower() for t in (cfg.get("cert_transparency", {}) or {}).get("hot_tokens", [])]
    old_subs = set((old.get("ct", {}) or {}).get("subdomains", []))
    new_subs = set((new.get("ct", {}) or {}).get("subdomains", []))
    if old.get("ct"):  # only after a baseline exists
        for s in sorted(new_subs - old_subs):
            # ONLY GTA6-relevant subdomains alert. Backend/infra subdomains
            # (prod.*.pod, ingest, telemetry, etc.) are silently baselined --
            # not what we're watching for, and crt.sh churns its result set.
            if any(tok in s for tok in hot):
                add("hot_subdomain", s, f"NEW GTA6-relevant subdomain: {s}")

    # build manifest: a new declared route = a page built (maybe unlinked)
    old_mr = set((old.get("manifest_routes", {}) or {}).get("routes", []))
    new_mr = set((new.get("manifest_routes", {}) or {}).get("routes", []))
    if old.get("manifest_routes"):
        for r in sorted(new_mr - old_mr):
            add("new_manifest_route", r, f"route declared in build manifest (maybe unlinked): {r}")
        ob = (old.get("manifest_routes", {}) or {}).get("buildId")
        nb = (new.get("manifest_routes", {}) or {}).get("buildId")
        if ob and nb and ob != nb:
            add("bundle_hash_changed", "buildId", f"site rebuilt: buildId {ob} -> {nb}")

    # discovery: NEW official urls anywhere (only GTA6-relevant ones alert)
    disc_hot = [str(t).lower() for t in (cfg.get("discovery", {}) or {}).get("hot_tokens", [])]
    old_disc = set((old.get("discovery", {}) or {}).get("urls", []))
    new_disc = set((new.get("discovery", {}) or {}).get("urls", []))
    if old.get("discovery"):
        for u in sorted(new_disc - old_disc):
            if any(tok in u.lower() for tok in disc_hot):
                add("new_official_url", u, f"NEW official URL discovered (GTA6-relevant): {u}")

    return changes


# --------------------------------------------------------------------------- #
# Persistence + outputs
# --------------------------------------------------------------------------- #
def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def write_dashboard_data(snap, changelog, status):
    knowledge = load_json(STATE / "knowledge.json", {})
    payload = {
        "generated": now_iso(),
        "knowledge": knowledge,
        "changelog": changelog[-200:],
        "status": status,
        "watched": {
            "pages": list((snap or {}).get("pages", {}).keys()),
            "probes": (snap or {}).get("probes", {}),
            "downloads": (snap or {}).get("downloads", {}),
            "deploy": {pid: f.get("fingerprint") for pid, f in (snap or {}).get("pages", {}).items()},
        },
    }
    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS / "data.js").write_text(
        "window.GTA6_DATA = " + json.dumps(payload, indent=2, ensure_ascii=False) + ";\n",
        encoding="utf-8")


# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #
def sev_ge(a, b):
    return SEV_ORDER.index(a) >= SEV_ORDER.index(b)


def fire_alerts(changes, threshold):
    notable = [c for c in changes if c["severity"] in SEV_ORDER and sev_ge(c["severity"], threshold)]
    if not notable:
        print(f"[{now_iso()}] {len(changes)} change(s), none >= {threshold}. Quiet.")
        return
    lines = [f"GTA6-WATCH: {len(notable)} signal(s) >= {threshold}"]
    for c in notable:
        lines.append(f"  [{c['severity']}] {c['type']} @ {c['target']} -- {c['detail']}")
    msg = "\n".join(lines)
    print(msg)

    # GitHub issue
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if token and repo and requests:
        top = max(notable, key=lambda c: SEV_ORDER.index(c["severity"]))
        title = f"[{top['severity']}] GTA6 signal: {top['type']} ({len(notable)} total)"
        try:
            requests.post(
                f"https://api.github.com/repos/{repo}/issues",
                headers={"Authorization": f"Bearer {token}",
                         "Accept": "application/vnd.github+json"},
                json={"title": title, "body": "```\n" + msg + "\n```",
                      "labels": ["gta6-signal", top["severity"].lower()]},
                timeout=20)
            print("  -> GitHub issue created")
        except Exception as exc:
            print(f"  -> GitHub issue failed: {exc}")

    # Webhook (Discord/Slack compatible)
    hook = os.environ.get("ALERT_WEBHOOK")
    if hook and requests:
        try:
            requests.post(hook, json={"content": msg, "text": msg}, timeout=20)
            print("  -> webhook posted")
        except Exception as exc:
            print(f"  -> webhook failed: {exc}")


# --------------------------------------------------------------------------- #
# Self-test (offline)
# --------------------------------------------------------------------------- #
def selftest(cfg):
    print("Running offline self-test of the diff/classify engine...")
    html_v1 = (
        "<html><head><title>GTA VI</title>"
        "<meta name='description' content='Coming 2026'>"
        "<meta property='og:image' content='https://x/share-v1.jpg'>"
        "</head><body>"
        "<a href='/VI/media'>Media</a><a href='/VI/editions'>Editions</a>"
        "<img src='/VI/media/shot1.jpg'><script src='/_next/app.js'></script>"
        "</body></html>"
    )
    html_v2 = (
        "<html><head><title>GTA VI - Pre-Order Now</title>"
        "<meta name='description' content='Coming November 19, 2026'>"
        "<meta property='og:image' content='https://x/share-v2.jpg'>"
        "</head><body>"
        "<a href='/VI/media'>Media</a><a href='/VI/editions'>Editions</a>"
        "<a href='/VI/pc'>PC</a>"
        "<img src='/VI/media/shot1.jpg'><img src='/VI/media/shot2.jpg'>"
        "<script src='/_next/app.js'></script></body></html>"
    )
    ex = ["title", "meta", "og", "routes", "media", "media_count", "bundles", "fingerprint"]
    dl1 = {"shots.zip": {"status": 200, "size": "100", "etag": "a"},
           "trailer3.zip": {"status": 404}}
    dl2 = {"shots.zip": {"status": 200, "size": "120", "etag": "b"},
           "trailer3.zip": {"status": 200, "size": "50", "etag": "c"}}
    si1 = {"og.jpg": {"etag": "x"}}
    si2 = {"og.jpg": {"etag": "y"}}
    v1 = {"pages": {"home": parse_page(html_v1, {}, ex)},
          "bundle_findings": grep_blob("var x='coming soon'", cfg),
          "probes": {"/VI/pc": 404}, "bundles": {}, "sitemap_urls": [], "newswire": [],
          "downloads": dl1, "share_images": si1}
    v2 = {"pages": {"home": parse_page(html_v2, {}, ex)},
          "bundle_findings": grep_blob("steam pc system requirements $99.99 2027", cfg),
          "probes": {"/VI/pc": 200}, "bundles": {}, "sitemap_urls": [], "newswire": [],
          "downloads": dl2, "share_images": si2}
    changes = diff(v1, v2, cfg)
    by_type = {c["type"]: c["severity"] for c in changes}
    print(f"  detected {len(changes)} changes:")
    for c in changes:
        print(f"    [{c['severity']}] {c['type']} -- {c['detail']}")
    expected = {
        "title_changed": "HIGH", "desc_changed": "HIGH", "og_image_changed": "HIGH",
        "new_route": "HIGH", "new_media_asset": "MEDIUM",
        "new_probe_live": "CRITICAL", "critical_keyword_new": "CRITICAL",
        "price_string_new": "HIGH", "date_string_new": "HIGH",
        "download_changed": "HIGH", "new_download_live": "CRITICAL",
        "share_image_changed": "HIGH",
    }
    ok = True
    for t, sev in expected.items():
        if by_type.get(t) != sev:
            print(f"  FAIL: expected {t} -> {sev}, got {by_type.get(t)}")
            ok = False
    print("  SELF-TEST PASSED" if ok else "  SELF-TEST FAILED")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="gta6-watch monitor")
    ap.add_argument("--init", action="store_true", help="capture baseline, no alerts")
    ap.add_argument("--dry-run", action="store_true", help="scrape+diff+print, no persist/alert")
    ap.add_argument("--selftest", action="store_true", help="offline engine test")
    ap.add_argument("--config", default=str(ROOT / "targets.yaml"))
    args = ap.parse_args()

    cfg = load_config(Path(args.config))

    if args.selftest:
        return selftest(cfg)

    if requests is None:
        raise SystemExit("requests is required: pip install requests")

    threshold = cfg.get("alert_threshold", "HIGH")
    prev = load_json(STATE / "snapshot.json", {})
    snap = scrape(cfg)
    changes = diff(prev, snap, cfg) if prev else []

    if args.dry_run:
        print(f"[dry-run] {len(changes)} change(s):")
        for c in changes:
            print(f"  [{c['severity']}] {c['type']} @ {c['target']} -- {c['detail']}")
        if snap.get("errors"):
            print("errors:", snap["errors"])
        return 0

    changelog = load_json(STATE / "changelog.json", [])
    if not prev or args.init:
        print(f"[{now_iso()}] Baseline captured ({len(snap.get('pages', {}))} pages). No alerts on init.")
    else:
        changelog.extend(changes)
        fire_alerts(changes, threshold)

    status = {"last_run": now_iso(), "changes_this_run": len(changes),
              "errors": snap.get("errors", []),
              "probes": snap.get("probes", {}),
              "total_changes_logged": len(changelog)}

    write_json(STATE / "snapshot.json", snap)
    write_json(STATE / "changelog.json", changelog)
    write_json(STATE / "status.json", status)
    write_dashboard_data(snap, changelog, status)
    print(f"[{now_iso()}] Run complete. {len(changes)} change(s) recorded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""PennyHunter — scrape community penny lists, alert on new SKUs, rebuild the site.

Why this shape:
  The original watcher was a long-running loop on a laptop that pushed one ntfy
  notification per SKU and stored its state in a Supabase table. That meant the
  Mac had to be awake, a bad scrape could fire 40 notifications at once, and the
  public site needed an API key embedded in the page to read the data.

  This version is a single stateless run: read state from data/finds.json,
  scrape, diff, write state back, regenerate docs/index.html, send ONE digest.
  GitHub Actions runs it on a schedule and commits the result, so the site is a
  plain static file with no keys in it and nothing to keep running.

  State lives in git, so every find is permanently versioned for free.

Usage:
  python3 hunt.py              # full run: scrape, write state + site, push digest
  python3 hunt.py --dry-run    # scrape and report only — writes nothing, pushes nothing
  python3 hunt.py --no-push    # scrape and write, but stay silent
"""
import argparse
import html
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
STORE_FILE = ROOT / "data" / "finds.json"
TEMPLATE_FILE = ROOT / "templates" / "index.html"
SITE_FILE = ROOT / "docs" / "index.html"

# The ntfy topic is effectively a password — anyone who knows it can push to the
# phone — so it is never committed. Locally: export NTFY_TOPIC=...
# In CI: repo secret NTFY_TOPIC.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
SITE_URL = os.environ.get("SITE_URL", "https://yas03n.github.io/penny-hunter/").strip()

HEADERS = {"User-Agent": "Mozilla/5.0 (personal penny-list reader)"}
REQUEST_TIMEOUT = 30

SOURCES = [
    {
        "name": "PennyCentral",
        "url": "https://www.pennycentral.com/penny-list",
        "link": "https://www.pennycentral.com/penny-list",
    },
    {
        "name": "Penny Pinchin' Mom",
        "url": "https://pennypinchinmom.com/home-depot-penny-list/",
        "link": "https://pennypinchinmom.com/home-depot-penny-list/",
    },
]

# Home Depot SKUs look like 1004-123-456. The leading "10" is what keeps this
# from matching phone numbers and prices scattered through the page text.
SKU_RE = re.compile(r"\b(10\d{2})[- ]?(\d{3})[- ]?(\d{3})\b")

# Trailing table-header noise that sits between the product name and the SKU.
LABEL_TAIL_RE = re.compile(r"[\s|·,–-]*(SKU|UPC|Internet\s*#|Model\s*#|Store\s*SKU|Item\s*#)\s*:?\s*$", re.I)

# Leading punctuation left behind after slicing at a boundary marker.
LABEL_HEAD_RE = re.compile(r"^[\s|·,:–-]+")

# How many characters before a SKU to grab as its product label.
LABEL_WINDOW = 140

# Where the *previous* listing ends and this product's name begins.
#
# The two sources lay their rows out differently:
#   PennyCentral    "…1 Report Find Home Depot Check Amazon | NAME | $0.01 $11.98 SKU 1009-583-946"
#   Penny Pinchin'  "…PREV NAME – SKU 1011-163-484 | NAME | SKU: 1005-780-718"
# In both cases the product name starts right after the last of these markers,
# so slicing at the final match isolates the correct name in one rule.
# The trailing [\d-] class deliberately excludes whitespace: allowing it let the
# match run past the SKU and swallow the next product's leading "3" in "3/4 in.".
# [A-Z]? absorbs a typo that appears live on Penny Pinchin' Mom ("SKUL 1010-…").
LABEL_BOUNDARY_RE = re.compile(
    r"(?:Check\s+Amazon"
    r"|Report\s+Find"
    r"|(?:SKU|UPC)[A-Z]?\s*:?\s*\d[\d-]{5,}"
    r"|\b10\d{2}-\d{3}-\d{3}\b"              # a bare SKU always ends the previous row
    r"|Page\s+\d+\s+of\s+\d+\s+\w+"          # pagination above the first listing
    r"|Deals\s+(?:for|Starting)\s+(?:the\s+week\s+of\s+)?\d{1,2}/\d{1,2}/\d{2,4})",
    re.I,
)

MAX_ROWS_ON_SITE = 40
MAX_ITEMS_IN_PUSH = 5


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_store():
    """Return {sku: record}. Missing or corrupt state starts empty rather than crashing."""
    if not STORE_FILE.exists():
        return {}
    try:
        return json.loads(STORE_FILE.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"  ! could not read {STORE_FILE.name} ({e}) — starting from empty state")
        return {}


def save_store(store):
    """Write state sorted by SKU so git diffs only show what actually changed."""
    ordered = {sku: store[sku] for sku in sorted(store)}
    STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STORE_FILE.write_text(json.dumps(ordered, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# Scrape
# ---------------------------------------------------------------------------

def clean_label(raw):
    """Normalise scraped text into something readable.

    Only whitespace, HTML entities and trailing table-header noise are touched —
    nothing that could drop a real word. The old dump stored labels with entities
    still in them (&quot;), so decoding here keeps the site escaping them once.
    """
    label = html.unescape(re.sub(r"\s+", " ", raw).strip())
    label = LABEL_TAIL_RE.sub("", label).strip()
    label = LABEL_HEAD_RE.sub("", label).strip()
    return label or "(no label)"


def extract_finds(page_html):
    """Return {sku: label} for every Home Depot SKU mentioned on the page."""
    text = BeautifulSoup(page_html, "html.parser").get_text(" ", strip=True)
    finds = {}
    for m in SKU_RE.finditer(text):
        sku = "-".join(m.groups())
        window_start = max(0, m.start() - LABEL_WINDOW)
        raw = text[window_start:m.start()]

        # Cut away the previous listing if its tail bled into the window.
        boundaries = list(LABEL_BOUNDARY_RE.finditer(raw))
        if boundaries:
            raw = raw[boundaries[-1].end():]
        elif window_start > 0 and not text[window_start - 1].isspace() and " " in raw:
            # No marker found and the window is a fixed character count, so it
            # may start mid-word. Drop that partial token only when we can prove
            # it is one — a length-based guess used to eat brands like "PACKOUT".
            raw = raw.split(" ", 1)[1]

        finds[sku] = clean_label(raw)
    return finds


def scrape_all():
    """Scrape every source. Returns (finds, failures).

    A source that fails is reported but never aborts the run — one dead site
    should not stop alerts from the other one.
    """
    finds, failures = {}, []
    for src in SOURCES:
        try:
            r = requests.get(src["url"], headers=HEADERS, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"[{src['name']}] fetch failed: {e}")
            failures.append(src["name"])
            continue
        found = extract_finds(r.text)
        print(f"[{src['name']}] {len(found)} SKUs on page")
        for sku, label in found.items():
            # First source to report a SKU keeps the attribution.
            finds.setdefault(sku, {"label": label, "source": src["name"], "url": src["link"]})
    return finds, failures


def merge(store, scraped, now_iso):
    """Fold a scrape into stored state. Returns the list of newly-seen SKUs."""
    new_skus = []
    for sku, info in scraped.items():
        if sku in store:
            # Already known — just record that it is still on the list today.
            store[sku]["last_seen"] = now_iso
            # Backfill a better label if we previously stored a useless one.
            if store[sku].get("label", "") in ("", "(no label)") and info["label"] != "(no label)":
                store[sku]["label"] = info["label"]
        else:
            store[sku] = {**info, "first_seen": now_iso, "last_seen": now_iso}
            new_skus.append(sku)
    return new_skus


# ---------------------------------------------------------------------------
# Site
# ---------------------------------------------------------------------------

def la_stamp(dt):
    """Format a UTC datetime as Pacific time — the only timezone this project cares about."""
    try:
        from zoneinfo import ZoneInfo
        local = dt.astimezone(ZoneInfo("America/Los_Angeles"))
        return local.strftime("%b %-d, %-I:%M %p") + " PT"
    except Exception:
        return dt.strftime("%b %-d, %-I:%M %p") + " UTC"


def short_date(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%b %-d")
    except (ValueError, AttributeError):
        return "—"


def build_site(store, new_count, failures, now):
    """Render docs/index.html from the template with the finds baked in.

    Baking the rows at build time is what removes the API key from the page:
    the browser fetches nothing, so there is nothing to authenticate.
    """
    newest = sorted(store.items(), key=lambda kv: kv[1].get("first_seen", ""), reverse=True)

    rows = []
    for sku, f in newest[:MAX_ROWS_ON_SITE]:
        digits = sku.replace("-", "")
        rows.append(
            "<tr>"
            f'<td>{html.escape(f.get("label", ""))}</td>'
            f'<td class="sku">{html.escape(sku)}</td>'
            f'<td class="src">{html.escape(f.get("source", ""))}</td>'
            f'<td class="src">{short_date(f.get("first_seen", ""))}</td>'
            f'<td><a class="check" target="_blank" rel="noopener" '
            f'href="https://www.homedepot.com/s/{digits}">Check stock &#8594;</a></td>'
            "</tr>"
        )

    if new_count:
        summary = (
            f"{new_count} new SKU{'s' if new_count != 1 else ''} since the last sweep. "
            "Sorted newest first — only an in-store UPC scan confirms a penny."
        )
    else:
        summary = (
            "No new SKUs this sweep. The list below is everything currently tracked, "
            "newest first — only an in-store UPC scan confirms a penny."
        )
    if failures:
        summary += f" (Source unreachable this run: {', '.join(failures)}.)"

    page = TEMPLATE_FILE.read_text()
    page = page.replace("{{COUNT}}", str(len(store)))
    page = page.replace("{{STAMP}}", f"Last swept {la_stamp(now)} · refreshes 4 AM &amp; 1 PM PT")
    page = page.replace("{{SUMMARY}}", summary)
    page = page.replace("{{ROWS}}", "".join(rows))

    SITE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SITE_FILE.write_text(page)
    return len(rows)


# ---------------------------------------------------------------------------
# Notify
# ---------------------------------------------------------------------------

def notify(store, new_skus, failures):
    """Send exactly one ntfy digest per run.

    One digest instead of one-push-per-SKU: a list refresh can surface 40 SKUs at
    once, and 40 buzzing notifications at 4 AM is how you stop reading them.
    """
    if not NTFY_TOPIC:
        print("  ! NTFY_TOPIC not set — skipping push (set it to get phone alerts)")
        return False

    if new_skus:
        title = f"{len(new_skus)} new penny lead{'s' if len(new_skus) != 1 else ''}"
        lines = []
        for sku in new_skus[:MAX_ITEMS_IN_PUSH]:
            lines.append(f"• {sku} — {store[sku]['label'][:70]}")
        if len(new_skus) > MAX_ITEMS_IN_PUSH:
            lines.append(f"…and {len(new_skus) - MAX_ITEMS_IN_PUSH} more")
        lines.append("")
        lines.append("Scan the item's UPC in-store to confirm. Stores: #8525 · #603 · #6664")
        body, priority, tags = "\n".join(lines), "high", "moneybag"
    else:
        title = "No new penny leads"
        body = f"{len(store)} SKUs still tracked. Tap to open the list."
        priority, tags = "low", "mag"

    if failures:
        body += f"\n⚠️ Could not reach: {', '.join(failures)}"

    try:
        r = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": priority,
                "Tags": tags,
                "Click": SITE_URL,
            },
            timeout=15,
        )
        r.raise_for_status()
        print(f"  → pushed: {title}")
        return True
    except requests.RequestException as e:
        print(f"  ! push failed: {e}")
        return False


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Scrape penny lists, alert, rebuild the site.")
    ap.add_argument("--dry-run", action="store_true", help="scrape and report only — no writes, no push")
    ap.add_argument("--no-push", action="store_true", help="write state and site but send no notification")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")

    store = load_store()
    print(f"State: {len(store)} SKUs known")

    scraped, failures = scrape_all()
    if not scraped and failures:
        # Every source failed. Don't rewrite state or the site off a bad run.
        print("All sources failed — leaving state and site untouched.")
        if not args.dry_run and not args.no_push:
            notify(store, [], failures)
        return 1

    new_skus = merge(store, scraped, now_iso)
    print(f"New this run: {len(new_skus)}")
    for sku in new_skus:
        print(f"  + {sku} — {store[sku]['label'][:70]}")

    if args.dry_run:
        print("Dry run — nothing written, nothing pushed.")
        return 0

    save_store(store)
    rendered = build_site(store, len(new_skus), failures, now)
    print(f"Wrote {STORE_FILE.relative_to(ROOT)} ({len(store)} SKUs) "
          f"and {SITE_FILE.relative_to(ROOT)} ({rendered} rows)")

    if not args.no_push:
        notify(store, new_skus, failures)
    return 0


if __name__ == "__main__":
    sys.exit(main())

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

# Both sources are NATIONAL lists — an item on them was pennied *somewhere*,
# not necessarily in Irvine. PennyCentral's ?state= filter is server-side
# (verified 2026-08-17: CA returns 12 SKUs, TX 13, different sets), so the
# CA page is the authoritative "confirmed ringing $0.01 in California" set —
# the closest signal to Irvine short of scanning the shelf. It also reaches
# SKUs on PennyCentral's later pages that the main scrape never sees.
HOME_STATE = "CA"
PC_STATE_URL = f"https://www.pennycentral.com/penny-list?state={HOME_STATE}"

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

MAX_ROWS_ON_SITE = 60
MAX_ITEMS_IN_PUSH = 5

# Sweep cadence, for display only — the real schedule lives in the workflow cron.
# Keep the two in step if you change one.
CHECK_EVERY_MIN = 10


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


# PennyCentral prints community metadata right after each SKU:
#   "SKU 1009-074-829 Last seen: Today Community 205 reports 31 states
#    Reported by dealsincali CA 35 NY 20 TX 18 FL 14 + 27 more Report Find"
# The state pairs are only the top few — CA can hide inside "+ 27 more", which
# is why membership in the ?state=CA page, not this text, decides ca_confirmed.
META_TAIL_RE = re.compile(
    r"\s*Last seen:.{0,20}?Community\s+(\d+)\s+reports?\s+(\d+)\s+states?"
    r"(?:\s+Reported by \S+)?((?:\s+[A-Z]{2}\s+\d+)+)?",
)
STATE_PAIR_RE = re.compile(r"([A-Z]{2})\s+(\d+)")


def extract_finds(page_html):
    """Return {sku: {label, reports?, state_counts?}} for every SKU on the page."""
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

        entry = {"label": clean_label(raw)}
        meta = META_TAIL_RE.match(text, m.end())
        if meta:
            entry["reports"] = int(meta.group(1))
            if meta.group(3):
                entry["state_counts"] = dict(
                    (st, int(n)) for st, n in STATE_PAIR_RE.findall(meta.group(3))
                )
        finds[sku] = entry
    return finds


def _to_record(entry, src_name, src_link):
    """Turn an extract_finds() entry into a storeable find record."""
    name, price, retail = split_price(entry["label"])
    rec = {
        "label": name,
        "price": price,
        "retail": retail,
        "source": src_name,
        "url": src_link,
    }
    if "reports" in entry:
        rec["reports"] = entry["reports"]
        ca_n = (entry.get("state_counts") or {}).get(HOME_STATE)
        if ca_n:
            rec["ca_reports"] = ca_n
    return rec


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
        for sku, entry in found.items():
            # First source to report a SKU keeps the attribution.
            finds.setdefault(sku, _to_record(entry, src["name"], src["link"]))

        # The state-filtered page settles "confirmed in my state?" for every
        # PennyCentral item — and surfaces state-confirmed SKUs from pages the
        # main scrape never reaches. Skipped silently on failure: ca stays at
        # its last known value rather than being wrongly cleared.
        if src["name"] == "PennyCentral":
            try:
                rs = requests.get(PC_STATE_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT)
                rs.raise_for_status()
                state_finds = extract_finds(rs.text)
                print(f"[PennyCentral {HOME_STATE}] {len(state_finds)} SKUs confirmed in {HOME_STATE}")
                for sku, entry in state_finds.items():
                    finds.setdefault(sku, _to_record(entry, src["name"], src["link"]))
                    finds[sku]["ca"] = True
                for sku in found:
                    finds[sku].setdefault("ca", False)
            except requests.RequestException as e:
                print(f"[PennyCentral {HOME_STATE}] fetch failed ({e}) — keeping stored ca flags")
    return finds, failures


def has_changed(store, scraped):
    """True if this scrape differs from the last one that was written.

    Running every 10 minutes means ~144 sweeps a day, and writing on every sweep
    would bury the repo in commits that say nothing. The previous live set is
    derived from the records whose last_seen matches the newest write, so no
    extra bookkeeping is needed — and comparing (price, ca) per SKU catches a
    SKU appearing, a SKU dropping off, a price moving, and a California
    confirmation flipping. Report COUNTS are deliberately excluded: they tick
    up constantly and would reintroduce the commit spam this check prevents.
    """
    def signature(rec):
        return (rec.get("price"), rec.get("ca"))

    latest = max((f.get("last_seen", "") for f in store.values()), default="")
    was = {sku: signature(f) for sku, f in store.items() if f.get("last_seen", "") == latest}
    now = {}
    for sku, info in scraped.items():
        # A failed state-page fetch omits "ca"; fall back to the stored flag so
        # the comparison sees "unchanged" rather than a phantom flip.
        sig = signature(info)
        if "ca" not in info and sku in store:
            sig = (sig[0], store[sku].get("ca"))
        now[sku] = sig
    return was != now


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
            # A live price always beats a stored one — this is how a price change
            # on the source page reaches the site.
            store[sku]["price"] = info["price"]
            if info.get("retail"):
                store[sku]["retail"] = info["retail"]
            # Same for community-report metadata. "ca" is only touched when the
            # scrape actually carried it (the state-page fetch succeeded), so a
            # transient failure never wrongly clears a confirmation.
            for k in ("reports", "ca_reports", "ca"):
                if k in info:
                    store[sku][k] = info[k]
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


def next_sweep(now):
    """Return the next 4 AM / 1 PM Pacific sweep as a friendly string.

    Shown on the site so a quiet day reads as "nothing new yet" rather than
    "this page is broken" — the complaint that prompted adding it.
    """
    try:
        from zoneinfo import ZoneInfo
        la = ZoneInfo("America/Los_Angeles")
    except Exception:
        return "4 AM & 1 PM PT"

    local = now.astimezone(la)
    for hour in (4, 13):
        if local.hour < hour:
            when = local.replace(hour=hour, minute=0, second=0, microsecond=0)
            return when.strftime("%-I:%M %p") + " PT today"
    return "4:00 AM PT tomorrow"


# PennyCentral ends each row with the current price then the retail price
# ("… $0.01 $199.00"). Both numbers are captured rather than assuming the first
# is always a penny, so if a source ever lists a $0.03 item the card shows the
# truth instead of a hardcoded lie.
PRICE_PAIR_RE = re.compile(r"\s*\$([\d,]+\.\d{2})\s+\$([\d,]+\.\d{2})\s*$")

# Penny Pinchin' Mom publishes no prices at all — its page is titled "Home Depot
# Penny List" and every row on it is claimed to ring at a penny. So an item with
# no published price is still a penny; that is what the source asserts.
DEFAULT_PRICE = "0.01"


def split_price(label):
    """Return (name, price, retail_or_None) from a scraped label."""
    m = PRICE_PAIR_RE.search(label)
    if m:
        return label[:m.start()].strip(" .…–-"), m.group(1), m.group(2)
    return label.strip(" .…–-"), DEFAULT_PRICE, None


def priced(record):
    """Read price fields off a record, deriving them for pre-migration rows."""
    if record.get("price"):
        return record.get("label", ""), record["price"], record.get("retail")
    return split_price(record.get("label", ""))


def build_site(store, new_count, failures, now):
    """Render docs/index.html from the template with the finds baked in.

    Baking the rows at build time is what removes the API key from the page:
    the browser fetches nothing, so there is nothing to authenticate.
    """
    # "Still on the list" is the distinction that actually decides whether a lead
    # is worth driving to, so sort on it first and label it — ordering purely by
    # first_seen made every migrated SKU look equally stale.
    latest_sweep = max((f.get("last_seen", "") for f in store.values()), default="")
    ranked = sorted(
        store.items(),
        key=lambda kv: (kv[1].get("last_seen", "") == latest_sweep, kv[1].get("first_seen", "")),
        reverse=True,
    )

    rows = []
    for sku, f in ranked[:MAX_ROWS_ON_SITE]:
        digits = sku.replace("-", "")
        name, price, retail = priced(f)
        live = f.get("last_seen", "") == latest_sweep

        # The price is the headline: it is the reason to drive to the store.
        price_block = f'<span class="now">${html.escape(price)}</span>'
        if retail:
            price_block += f'<span class="was">${html.escape(retail)}</span>'
            try:
                saved = float(retail.replace(",", "")) - float(price.replace(",", ""))
                price_block += f'<span class="save">save ${saved:,.2f}</span>'
            except ValueError:
                pass

        # Where has it actually been confirmed? Three honest states:
        #   confirmed in CA / national reports but no CA yet / source has no data.
        if f.get("ca"):
            n = f.get("ca_reports")
            where = ('<span class="ca-yes">✓ confirmed in CA'
                     + (f" · {n} report{'s' if n != 1 else ''}" if n else "")
                     + "</span>")
        elif f.get("reports"):
            where = (f'<span class="ca-no">no CA reports yet · '
                     f'{f["reports"]} nationwide</span>')
        else:
            where = '<span class="ca-no">no location data</span>'

        rows.append(
            '<article class="find">'
            '<div class="find-body">'
            f"<h3>{html.escape(name)}</h3>"
            f'<div class="price">{price_block}</div>'
            '<div class="find-meta">'
            + (
                '<span class="live">● on list now</span>'
                if live
                else f'<span class="gone">○ gone since {short_date(f.get("last_seen", ""))}</span>'
            )
            + where
            + f'<span class="sku">{html.escape(sku)}</span>'
            f"<span>{html.escape(f.get('source', ''))}</span>"
            f"<span>added {short_date(f.get('first_seen', ''))}</span>"
            + "</div></div>"
            f'<a class="go" target="_blank" rel="noopener" '
            f'href="https://www.homedepot.com/s/{digits}">Check stock &#8594;</a>'
            "</article>"
        )

    if new_count:
        summary = (
            f"{new_count} new SKU{'s' if new_count != 1 else ''} on the last sweep — "
            f"your phone was alerted the moment they appeared. Dates are when a SKU first "
            "showed up on the lists, not when it was last checked."
        )
    else:
        summary = (
            f"The lists are checked every {CHECK_EVERY_MIN} minutes and you are alerted the "
            "moment anything new appears. This page only changes when the lists do, so an "
            "older timestamp means nothing has moved — not that it has stopped."
        )
    if failures:
        summary += f" Source unreachable this run: {', '.join(failures)}."

    page = TEMPLATE_FILE.read_text()
    page = page.replace("{{COUNT}}", str(len(store)))
    page = page.replace("{{STAMP}}", la_stamp(now))
    page = page.replace("{{NEXT}}", f"every {CHECK_EVERY_MIN} min")
    page = page.replace("{{SUMMARY}}", summary)
    page = page.replace("{{CARDS}}", "".join(rows))

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
            ca_mark = " ✓CA" if store[sku].get("ca") else ""
            lines.append(f"• {sku}{ca_mark} — {store[sku]['label'][:70]}")
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
    ap.add_argument("--digest", action="store_true",
                    help="daily check-in: rebuild and notify even when nothing changed")
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

    # Decide before merging — merge() stamps last_seen and would erase the diff.
    changed = has_changed(store, scraped)

    new_skus = merge(store, scraped, now_iso)
    print(f"New this run: {len(new_skus)}  (lists changed: {changed})")
    for sku in new_skus:
        print(f"  + {sku} — {store[sku]['label'][:70]}")

    if args.dry_run:
        print("Dry run — nothing written, nothing pushed.")
        return 0

    # A sweep that found nothing new writes nothing at all. At ~144 sweeps a day
    # that keeps the repo history meaningful: a commit means the lists moved.
    if not changed and not args.digest:
        print("No change since the last write — leaving state and site untouched.")
        return 0

    save_store(store)
    rendered = build_site(store, len(new_skus), failures, now)
    print(f"Wrote {STORE_FILE.relative_to(ROOT)} ({len(store)} SKUs) "
          f"and {SITE_FILE.relative_to(ROOT)} ({rendered} rows)")

    # Alert on anything new the instant it appears; otherwise only on the twice-
    # daily check-in, so a quiet day is two notifications and not a hundred.
    if not args.no_push and (new_skus or args.digest):
        notify(store, new_skus, failures)
    return 0


if __name__ == "__main__":
    sys.exit(main())

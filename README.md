# PennyHunter

Every 10 minutes it scrapes the community Home Depot penny lists, alerts your
phone the moment anything new appears, and rebuilds a one-page site with the
current finds. Built for one person shopping Irvine, CA (92614).

**Site:** https://yas03n.github.io/penny-hunter/
**Sweeps:** every 10 minutes, plus a check-in digest at 4:00 AM and 1:00 PM Pacific.

A sweep that finds nothing writes nothing and sends nothing, so a commit here
means the lists actually moved. GitHub's scheduler is best-effort — runs can be
a few minutes late under load.

## How it works

```
GitHub Actions (cron)
  └─ hunt.py  → scrape 2 sources
              → diff against data/finds.json
              → push one ntfy digest to the phone
              → render templates/index.html into docs/index.html
  └─ commit the refreshed files back
GitHub Pages serves docs/
```

No database, no server, no API keys in the page. State is a JSON file in git, so
every find is permanently versioned for free.

## Running it locally

```bash
pip install -r requirements.txt
export NTFY_TOPIC=<your ntfy topic>   # only needed to test the phone push

python3 hunt.py --dry-run   # scrape and report — writes nothing, pushes nothing
python3 hunt.py --no-push   # write state + site, stay silent
python3 hunt.py             # a routine sweep: acts only if the lists moved
python3 hunt.py --digest    # check-in: rebuild and notify even if nothing changed
```

Then `open docs/index.html`.

## Layout

| Path | What it is |
|---|---|
| `hunt.py` | The whole program |
| `data/finds.json` | Every SKU ever seen, with first/last seen dates |
| `templates/index.html` | Site template |
| `docs/index.html` | Generated site — never hand-edit |
| `.github/workflows/hunt.yml` | The schedule |

## Configuration

| Name | Where | Purpose |
|---|---|---|
| `NTFY_TOPIC` | repo **secret** | ntfy topic the digest is pushed to. Never commit it. |
| `SITE_URL` | repo **variable** | Where the notification's tap-through points. |

## History

This started inside the **Ascend** Supabase project. A 2026-08-17 audit flagged
it — the tables were world-readable and the page shipped Ascend's publishable key
to read them — so it was exported and removed from Ascend entirely. It could not
get its own Supabase project (org is at the 2-free-project cap), which forced the
rewrite into a static site. That removed the key, the database and the server in
one move.

The pre-migration export lives in `archive/` on the original machine. It is
gitignored: it contains the old key and ~15 MB of compiled edge-function bundles.

## The disclaimer that matters

No retailer publishes penny prices. Everything here is community-reported, and
every lead is a lead, not a promise — **only an in-store UPC scan confirms a
penny.** Scan the item's own barcode, never the yellow sticker.

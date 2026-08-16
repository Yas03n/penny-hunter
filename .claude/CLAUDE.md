# PennyHunter — project instructions

Personal deal-hunting tool. It scrapes community-reported Home Depot penny SKUs
twice a day, pushes one digest to Yaseen's phone, and regenerates a static site
he keeps on his iPhone home screen. Irvine, CA (92614).

**Not** a product, not an app, no users but Yaseen. Optimise for "still working
in six months with zero maintenance", not for features.

---

## Architecture in one breath

```
GitHub Actions (cron, every 10 min — 144 sweeps/day)
  └─ hunt.py
       ├─ reads   data/finds.json          ← state, versioned in git
       ├─ scrapes PennyCentral + Penny Pinchin' Mom
       ├─ has_changed()?  no  → exit, write nothing, say nothing
       │                  yes ↓
       ├─ writes  data/finds.json          ← + newly-seen SKUs
       ├─ renders templates/index.html → docs/index.html
       └─ pushes  ONE ntfy alert → phone
  └─ commits both files back to the repo
GitHub Pages serves docs/ → https://yas03n.github.io/penny-hunter/
```

Two of the 144 daily sweeps (4 AM and 1 PM Pacific) run with `--digest`, which
forces a rebuild and a notification even when nothing changed. That is both the
daily check-in and the proof the job is still alive.

There is **no database, no server, and no API key anywhere**. That is the whole
design. If a change would reintroduce any of the three, it is the wrong change.

## Why it looks like this

This project used to live inside the **Ascend** production Supabase project. The
2026-08-17 Ascend audit flagged it: `penny_finds`/`penny_meta` had world-readable
policies and the site shipped Ascend's publishable key in its HTML to read them.
Everything was exported, then deleted from Ascend.

It could not move to its own Supabase project — the org is capped at 2 free
projects (Ascend + nudge) — so it became a static site instead. That turned out
better: a scraper that runs twice a day and writes 91 rows never needed Postgres.

**Hard rule: nothing from this project ever goes back into the Ascend project.**

---

## Layout

| Path | What it is |
|---|---|
| `hunt.py` | The entire program. Scrape → diff → notify → render. |
| `data/finds.json` | State: `{sku: {label, source, url, first_seen, last_seen}}`. Sorted by SKU so git diffs stay readable. |
| `templates/index.html` | Site template. `{{COUNT}} {{STAMP}} {{NEXT}} {{SUMMARY}} {{CARDS}}`. |
| `docs/index.html` | **Generated — never hand-edit.** GitHub Pages serves this. |
| `.github/workflows/hunt.yml` | The 4 AM / 1 PM PT schedule. |
| `archive/` | Pre-migration Supabase export. **Gitignored**, local only. |

## Conventions

- **Python 3, stdlib + `requests` + `beautifulsoup4`.** Nothing else. If a change
  wants a third dependency, it probably wants to be a different project.
- Match the existing comment style: a "why" docblock at the top of each function
  that makes a non-obvious choice, and inline comments only where the code cannot
  speak for itself. Beginner-readable over clever.
- `data/finds.json` is written sorted by key. Keep it that way — the daily bot
  commit should show only genuinely changed rows.
- The site keeps the original hand-built page's colour tokens and feel. It is used
  almost entirely on an iPhone, so **mobile is the primary target** — verify at a
  ~390px width before anything else.
- **Never lay out finds as a `<table>`.** The first version did, and it forced the
  whole page to scroll sideways to reach the "Check stock" button. Finds are flex
  cards that wrap; under 520px the button drops to its own full-width row. `body`
  carries `overflow-x:hidden` as a backstop. Keep both.

## Secrets

`NTFY_TOPIC` is effectively a password — anyone who knows it can push
notifications to Yaseen's phone.

- **Never** commit it. It is read from the environment only.
- CI: repo secret `NTFY_TOPIC`. Local: `export NTFY_TOPIC=...`.
- The repo is **public**. Before adding any file, ask whether it would be fine on
  a billboard. Scraped public SKU data: fine. Anything else: probably not.

## Working on this

```bash
export NTFY_TOPIC=<topic>          # only needed if you want to test the push
python3 hunt.py --dry-run          # scrape + report, writes nothing, pushes nothing
python3 hunt.py --no-push          # writes state + site, stays silent
python3 hunt.py                    # the real thing
open docs/index.html               # eyeball the site
```

Always `--dry-run` first when touching the scraper. Then check `docs/index.html`
renders before committing.

### The label extractor is the fragile part

Labels are scraped from a fixed character window *before* each SKU, so most bugs
live in `LABEL_BOUNDARY_RE`. The two sources are laid out differently:

- **PennyCentral** — `…Report Find Home Depot Check Amazon | NAME | $0.01 $11.98 SKU 1009-583-946`
- **Penny Pinchin' Mom** — `…PREV NAME – SKU 1011-163-484 | NAME | SKU: 1005-780-718`

so the product name always begins right after the *last* boundary marker in the
window. Bugs already fixed here, do not reintroduce them:

- A length-based "drop the first token" guess ate real brands (`PACKOUT`).
- Allowing `\s` inside the SKU digit class swallowed the next name's `3` in `3/4 in.`.
- Penny Pinchin' Mom has a live typo, `SKUL 1010-…`, hence `[A-Z]?`.

After any change there, re-scrape both pages and read ~10 labels from each before
trusting it.

## Deliberate behaviours (don't "fix" these)

- **One digest per run, never one push per SKU.** A list refresh can surface 40
  SKUs at once; 40 buzzes at 4 AM is how you stop reading notifications.
- **A sweep that finds nothing writes nothing and says nothing.** At 144 sweeps a
  day, writing every time would bury the repo in empty commits and the phone in
  empty alerts. `has_changed()` compares `{sku: price}` against the last written
  state, which catches an arrival, a drop-off and a price move in one comparison
  — including the one-in-one-out case a count check would miss. **A commit in
  this repo therefore means the lists actually moved.**
- **`has_changed()` must be called before `merge()`**, which stamps `last_seen`
  and would erase the very difference being measured.
- **The 4 AM / 1 PM digests notify even when nothing is new.** That is the point
  — it confirms the job is alive. The gate is on the *Pacific* hour with
  `minute < 10`, so it survives DST without touching the cron and fires once per
  digest hour rather than on all six sweeps in it.
- **Actions cron is best-effort.** Runs queue under load and can land several
  minutes late or be skipped entirely; 10 minutes is the target, not a promise.
  Do not present it as a guarantee.
- **If every source fails, state and site are left untouched** and a warning is
  pushed. Never let a bad scrape wipe the finds.
- Historical SKUs stay in `data/finds.json` forever even after they drop off the
  lists. `last_seen` is how you tell live from stale.
- **The site sorts by "still on the list", not by date.** A SKU whose `last_seen`
  matches the newest sweep is ranked first and badged "on list now"; everything
  else is badged "gone since …". Sorting purely by `first_seen` made every
  migrated SKU look equally stale and read as a broken page.
- The status strip up top ("Last checked / Next check / Tracking") exists so a
  quiet day is legible as "nothing new" rather than "this is frozen". Don't
  remove it — that confusion is exactly what prompted it.

## Known limits

- GitHub disables scheduled workflows after ~60 days of repo inactivity. The
  bot's own commits normally keep it alive, and GitHub emails first — if alerts
  ever stop, check the Actions tab before debugging the scraper.
- Every lead is a lead, not a promise. Only an in-store UPC scan confirms a penny.
  Keep that disclaimer on the site.

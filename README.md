# ScoutForge

Roblox game scouting intelligence for Discord.

ScoutForge watches **public** Roblox data for games matching your criteria,
builds a full observation history for each one, scores them transparently, and
alerts you when a game starts getting hot — while you track outreach in a small
CRM.

It ships two things in one image: the Discord bot and the discovery worker
(headless, no Discord connection).

```
Discord ──▶ /scan, /hot, /leads, /lead, /partners …   (bot)
Worker  ──▶ discover → filter → snapshot → score → alert
```

---

## What it actually does

| Stage | Behaviour |
| --- | --- |
| **Discover** | Pluggable sources (`roblox_explore`, `roblox_search`, `roblox_similar`) read public endpoints. |
| **Filter** | Hard criteria — `MIN_CCU`, abandoned-collapse detection. The lifetime-visit ceiling is opt-in (`MAX_LIFETIME_VISITS=0` by default) so an established game is never rejected just for being big. |
| **Snapshot** | Every tracked game gets a timestamped CCU / visits / favourites row, so history is real. |
| **Score** | Transparent 0–100 from nine weighted components, with reasons *and* caveats. |
| **Match** | Every game is ranked against configurable partner profiles — with the exclusions that knocked a partner out. |
| **Alert** | Discord embeds at WARM / HOT / EXTREME, rate-limited by per-level cooldowns. |
| **Track** | CRM statuses, notes, assignments and a full audit trail per lead. |

### What it does not do

* **It does not predict commercial success.** The Scout Score measures observed
  attractiveness at a point in time. Treat it as a ranking aid.
* **It never invents history.** Growth is computed only from snapshots that were
  actually recorded. Metrics Roblox does not return stay `null` and are listed
  in `missing_metrics` instead of being guessed.
* **It never contacts anyone.** Outreach is always manual; the bot only tracks
  and reminds you.
* **It uses public Roblox APIs only.** No authentication bypass, no private
  endpoints, no scraping behind a login, no rate-limit circumvention.

---

## Quick start (Docker)

```bash
cp .env.example .env      # then fill in DISCORD_TOKEN and ALERT_CHANNEL_ID
docker compose up -d
```

That starts MySQL 8.4, applies migrations once, then runs the bot with the
scheduler.

Prefer scanning to survive Discord outages? Run the worker instead of the bot's
scheduler:

```bash
docker compose --profile worker up -d worker
```

Seed demo data to look around before pointing it at anything real:

```bash
docker compose exec bot scoutforge seed-demo
```

`ENVIRONMENT` defaults to `production`, so the seeder refuses a database that
looks real — including the compose `mysql` service. Set `ENVIRONMENT=development`
for a scratch database, or pass `--force` when you truly mean it. The guard runs
before any schema change, so a refusal never creates tables.

## Quick start (local)

Requires Python 3.12+ and a MySQL 8.x instance (8.4 recommended).

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

export DISCORD_TOKEN=...  ALERT_CHANNEL_ID=...
export DATABASE_URL="mysql+asyncmy://scoutforge:scoutforge@localhost:3306/scoutforge?charset=utf8mb4"

scoutforge --check          # validate configuration
scoutforge migrate          # apply migrations
scoutforge sync-commands    # register slash commands
scoutforge run              # bot + scheduler
```

`sqlite+aiosqlite://` works too if you just want to try it locally.

### Commands

| Command | What it does |
| --- | --- |
| `scoutforge run` | Bot and scan scheduler (default). |
| `scoutforge worker` | Headless discovery worker, no Discord. |
| `scoutforge sync-commands` | Register slash commands and exit. |
| `scoutforge migrate` | `alembic upgrade head`. |
| `scoutforge seed-demo [--force]` | Insert labelled demo data (`--force` overrides the production guard). |
| `scoutforge --check` | Validate configuration and exit. |

---

## Discord commands

**Discovery** — `/scan` run a scan now · `/search <query>` find tracked games ·
`/hot` current HOT/EXTREME leads · `/leads` browse the pipeline ·
`/lead <universe_id>` full detail · `/sources` source health

**CRM** — `/status <lead> <status>` move a lead · `/note <lead> <text>` attach a
note · `/history <lead>` status + note timeline · `/assign <lead>` assign a
scout · `/myqueue` your leads · `/why <lead>` score breakdown

**Partners** — `/partners` the profile set and its deal terms (add a partner key
to see its best fits) · `/match <lead>` who this game fits, and who passed ·
`/shortlist <lead> <partner>` mark a fit for outreach · `/outreach <lead>
<partner> <action>` log contact you made yourself

**Control** — `/alerts status` current configuration · `/alerts toggle <on\|off>` ·
`/alerts threshold hot/extreme/warm <score>` · `/alerts cooldown hot_hours
/ extreme_hours` · `/alerts history` · `/stats` telemetry · `/help`

Admin-only: `/alerts toggle`, `/alerts threshold` and `/alerts cooldown` require
`ADMIN_ROLE_IDS`.

Pipeline: `NEW → RESEARCHED → CONTACTED → REPLIED → INTERESTED →
SENT_TO_WORLDENT → NEGOTIATING → DEAL`, with `REJECTED` / `NOT_INTERESTED` as
terminal states.

---

## The Scout Score

A 0–100 weighted sum, published in full rather than hidden behind a model:

| Component | Default weight | What it measures |
| --- | --- | --- |
| Ratio | 22 | CCU relative to lifetime visits. |
| CCU | 16 | Absolute concurrent players. |
| Growth | 16 | Recent CCU trend over 1h/6h/24h. |
| Consistency | 10 | Stability of the CCU series. |
| Engagement | 8 | Favourites and likes as a share of visits. |
| Monetisation | 8 | Accumulated engagement plus monetisation vocabulary (gamepass, VIP, tycoon…). Always labelled a signal — Roblox publishes no revenue. |
| Freshness | 8 | How recently the game was discovered. |
| Footprint | 6 | Lifetime-visit scale. Gentle, with real headroom, so a proven multi-million-visit game is not zeroed out. |
| Originality | 6 | Copy/clone and genre-clone wording in the title. |

Unavailable components have their weight **redistributed**, never counted as
zero. Every score carries `reasons` (why it is high) and `caveats` (why it may
not be high) — for example *"no 24h history yet"*.

Levels: `NORMAL` < `WARM` ≥ 75 < `HOT` ≥ 90 < `EXTREME` ≥ 95.

## Partner matching

Every scored game is ranked against a set of **editable partner profiles**. A
profile is plain JSON, so deal terms can change without a deploy:

```json
{
  "profiles": [
    {
      "key": "xolbor",
      "name": "Xolbor",
      "revenue_share_min_pct": 2,
      "revenue_share_note": "2-5%+",
      "preferred_stages": ["early"],
      "preferred_ccu_min": 100,
      "preferred_visits_max": 1000000,
      "exclude_risk": ["mount", "modded", "dmca", "ugc", "tower", "anime", "copy"]
    }
  ]
}
```

Point `PARTNER_PROFILES_FILE` at it, or store the same document under the
`partner_profiles` settings row (which wins over the file). Defaults ship for
INDG, InCreates, Partner #3, Xolbor, CreatorExchange and Locked.dev. Profiles
are changed by editing the file or the settings row -
`PartnerService.save_profiles()` does the validating write for anything
programmatic.

Each match records the partner, a 0–100 compatibility score, the reasons behind
it, **and** the exclusions/mismatches for the partners that said no — so nothing
is dropped silently. Matches appear in the lead detail page, in `/partners` and
`/match`, and inside HOT/EXTREME alert embeds. Routing a lead to a partner is
manual: `/shortlist` and `/outreach` record who contacted whom, when, where and what
they replied.

Scouting data is public, so the matcher is a ranking aid for humans. ScoutForge
never messages a partner or a creator on your behalf.

## Alerting rules

Cooldowns stop alert spam: HOT 6h, EXTREME 3h, WARM 24h. Three situations
deliberately bypass a cooldown:

1. a `+5` point score jump,
2. a `+50%` CCU spike,
3. escalation into a higher level.

EXTREME-by-growth additionally requires **≥100%** change **and** **≥500**
absolute CCU within a 1h/6h/24h window, so a 5-player game cannot shout.

---

## Configuration

Every setting is an environment variable; see `.env.example` for the full list
with comments. The ones that matter most:

| Variable | Default | Notes |
| --- | --- | --- |
| `DISCORD_TOKEN` | — | Required. |
| `ALERT_CHANNEL_ID` | — | Required, or alerts cannot be delivered. |
| `DATABASE_URL` | — | `mysql+asyncmy://…` (MySQL 8.x) |
| `DB_ISOLATION_LEVEL` | `READ COMMITTED` | Keeps long scans from blocking short reads. |
| `MIN_CCU` | `1000` | Primary discovery filter. |
| `MAX_LIFETIME_VISITS` | `0` | Off by default. Set a number to hard-cap game size. |
| `PARTNER_MATCHING_ENABLED` | `true` | Rank games against partner profiles. |
| `PARTNER_PROFILES_FILE` | — | JSON file that replaces the built-in profiles. |
| `SCAN_INTERVAL_SECONDS` | `600` | Scheduler cadence. |
| `ROBLOX_RATE_LIMIT_RPS` | `8` | Requests/second/host. Be polite. |

---

## Development

```bash
pip install -e ".[dev]"

ruff check .              # lint
ruff format --check .     # formatting
mypy scoutforge           # types
pytest -q                 # tests
```

The suite runs fully offline: in-memory SQLite with the production schema,
`respx` for HTTP, and a frozen clock so growth and freshness maths are exact.

```
scoutforge/tests/
  conftest.py              shared fixtures
  unit/                    filters, growth, scoring, alerts, dedupe, CRM, utils,
                           HTTP client, embeds
  integration/             repositories and services against a real schema
```

### Layout

```
scoutforge/
  bot/          Discord client, commands, views, embeds, scheduler
  config/       pydantic settings and logging
  database/     models, repositories, migrations
  discovery/    sources and the discovery contract
  monitoring/   growth metrics and the alert engine
  net/          polite HTTP client (rate limiting, retries, backoff)
  partners/     partner profiles, keyword signals and the matcher
  scoring/      the Scout Score components and rules
  services/     application services wiring the stages together
```

---

## Ethics and compliance

ScoutForge reads public Roblox API responses and nothing else. It does not
circumvent access controls, does not scrape behind authentication, does not
mass-message anyone, and every outreach decision stays with a human. Roblox game
creators are contacted by you, at your discretion, about your own business
offer.

## Licence

MIT.
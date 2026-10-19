# DC Time Machine — SPX Paper Lab

A local, containerized research dashboard for a **10–14 trading-session entry
study**, followed by monitoring until the last position closes. **SPX only** in
this version, per the initial rollout decision. Uses **SPXW**, European-style,
100-multiplier, PM cash-settled options. SPY/QQQ and physical assignment are
deliberately not supported.

**No orders are submitted to IBKR**, on either port. This application simulates
fills from live quotes; it does not place orders in IBKR's paper account.
Neither a modeled zero-loss floor nor natural quotes guarantee executable fills.
This is not a certification of the named strategy or a recommendation to trade.

## Quick start — Linux / WSL2

From this directory:

```bash
cp .env.example .env
mkdir -p data/csv
# Container runs as UID/GID 10001; grant it access to this dedicated directory.
sudo chown -R 10001:10001 ./data
docker compose up --build -d
docker compose logs -f paper-lab
```

Open **http://localhost:8000**. The dashboard works without TWS, but displays
DISCONNECTED and cannot generate entries without valid live data.

```bash
curl http://localhost:8000/api/health
docker compose down
```

Only one application process/replica is supported; do not run the container and
native app simultaneously against the same data directory. Compose publishes
the web port on **127.0.0.1** only, with no public host listener. Do not put it
behind a public reverse proxy: it is a local-only tool without authentication.
The built-in health check reports app/SQLite readiness, not broker readiness;
`ibkr_connected` is reported separately.

### Docker startup troubleshooting

`IB_PORT` is supplied as a string by Docker and dotenv files. The application
converts it to an integer before validating the supported ports (`7497` or
`7496`). If an older image exits with `Input should be 7497 or 7496` despite
`IB_PORT=7497`, rebuild and recreate the service after applying the fix:

```bash
docker compose up --build --force-recreate -d paper-lab
docker compose logs --tail=100 paper-lab
curl http://localhost:8000/api/health
```

Do not remove the port setting or change to an unsupported port to bypass the
error. A subsequent `ibkr_connected: false` means the application started but
the TWS/Gateway socket connection still needs to be configured.

### TWS / IB Gateway

1. Log into TWS with the intended data-entitled user. Paper TWS normally uses
   **7497**; **7496** is live TWS, but this application still only reads data.
2. In **Global Configuration → API → Settings**, enable socket clients, set the
   port, enable **Read-Only API**, and trust only the needed local client.
   Keep API access restricted to your own host. Use a unique client ID (default
   71). IB Gateway's default ports are 4002/4001: configure its socket port to
   7497/7496 for this app.
3. Subscribe to live **SPX index and SPXW option** data and, optionally, VIX.
   Paper/live users can have different entitlement/sharing restrictions.
   Delayed, frozen, missing, crossed, stale or over-wide option quotes are
   rejected. IBKR's API must supply model Greeks for strike selection.
4. Keep TWS/Gateway running and handle its login/session resets. The app
   reconnects each polling cycle. Reconnection does **not** backfill missed
   entry, stop or transform opportunities.

**WSL networking:** Compose uses a bridge and
`extra_hosts: ["host.docker.internal:host-gateway"]`. The environment example
uses this alias for the broker. If TWS runs on Windows and that alias does not
reach it in your Docker/WSL setup, set `IB_HOST` to the reachable Windows host
address (or use supported WSL mirrored networking), and explicitly configure
the Windows firewall/TWS trusted client for the Docker/WSL client address.
Do not expose the API publicly. With native Linux TWS, a container cannot reach
a broker bound exclusively to host loopback: configure the broker to accept
only the trusted bridge client, or use host networking with `IB_HOST=127.0.0.1`
and a loopback-only Uvicorn listener.

### Native Python

Python **3.11+**:

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements-dev.txt
set -a
source .env
set +a
# Keep DATA_DIR absolute when changing the working directory.
export DATA_DIR="$PWD/data"
export IB_HOST=127.0.0.1
cd backend
../.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Use a user-writable data directory for native runs, rather than the
container-owned directory. SQLite and CSV data are never seeded with sample
performance. `pandas` is pinned below 3 because this exchange-calendar release
depends on its timestamp resolution behavior.

## How the study works

- `America/New_York` scheduling: **10:30 Eastern**, following daylight saving,
  not fixed UTC−5 year-round. XNYS sessions skip holidays and handle early closes.
  Conservative regular-hours monitoring stops at the equity-market close;
  SPXW may trade later on non-expiration days, which is outside this study.
- **10:25 preflight** retrieves the actual SMART SPXW chain, qualifies contracts,
  and selects expirations/strikes. Start before this time. No preflight means
  the scheduled entry is explicitly skipped rather than entered late.
- Front expiration is 6–12 **calendar** DTE; back expiration is 1–4 calendar days
  later. Friday/Monday is preferred **only if listed**. Sampling scans up to 16
  strikes per side within 6% of spot, selecting the closest live absolute delta
  to 0.35 in the 0.30–0.40 range. If the scan cannot qualify a valid basket, it
  logs an error instead of substituting invented dates or deltas.
- The 10:30 cron **starts** fresh quote sampling. Entry timestamp is actual
  sample completion, not a fabricated 10:30:00 fill. Selected short deltas are
  rechecked. All fills must occur in the 10:30 minute, or the entry is skipped.
  API/network latency means exact simultaneous 10:30:00 fills are not promised.
- One normal entry per SPX/session, enforced in both the engine and a SQLite partial
  unique index. Standard manual entry shares that limit, requires an open session, and
  uses the same live quote validation. No after-hours fictitious entries.
- Default debit is back-long **asks** minus front-short **bids**. Each simulated
  execution requires displayed size on the fill side sufficient for its entire
  quantity. This is a top-of-book model, not depth, queue or combo-fill simulation.
- Polling defaults to **20 seconds** (configurable 15–30). Each successful
  evaluation stores the full timestamped six-leg quote snapshot, IV, delta,
  bid/ask/mid, displayed sizes, transformer credit and liquidation value in SQLite.
  Condor marks store the remaining four legs. Unavailable data logs errors and
  never generates fills; the UI labels old marks stale. Sequential requests can
  take longer than the configured interval when IBKR is slow; missed polls
  coalesce, with no invented observations.
- Bid/ask ages use separately observed IBKR API tick reception timestamps.
  They do not prove exchange-origin freshness or synchronous execution across
  legs. Live market-data type 1 is mandatory. VIX is optional context: missing
  VIX is visibly logged, not replaced by a made-up value.
- Monitoring/settlement continues after the entry window. The window starts
  with the first successful entry and includes market sessions without a trade.
  Keep the app running for at least 10 sessions; 14 is the default upper bound.
  Closed-position statistics are not final until every open trade is resolved.

### Controlled manual retests

Use **Manual Retest** only for an explicitly confirmed extra plumbing/protection
test after today's normal SPX entry has closed. Every existing SPX position must
be closed, including positions from earlier sessions. The override never deletes,
reopens, or alters the original trade, and does not change the normal daily cap.
The enabled setting, study window, regular hours, live quote validation, quantity,
and 30% initial-loss guard still apply.

The dashboard requires a reason and the explicit **ONE extra SPX paper test**
confirmation checkbox. The dedicated `POST /api/entry/retest` endpoint requires:

```json
{
  "request_id": "7a94eb23-40dd-4abe-a0f1-f7adb53a5c76",
  "reason": "Observe 30% bot-monitored calendar protection",
  "confirmed": true
}
```

Generate a **new UUID for each intentional new test**, but reuse that exact UUID
and reason after a timeout or uncertain response. A repeated successful request
returns its existing trade, even after closure, restart, or market close, without
new fills. Reusing its ID with a different reason is rejected. The dashboard saves
pending requests in tab session storage, including across reloads; do not clear
this storage or change the UUID to bypass an ambiguous result.

Retests have `-TEST-<UUID>` IDs, persisted request/reason metadata, and
`MANUAL_RETEST` CSV notes. ZIP reports include `manual_retest_audit.json` with their
full metadata. The dashboard shows daily and retest counts separately.
**All aggregate performance metrics include retests**; these are not additional
10:30 study observations. Exported CSV column schemas remain unchanged.

On first startup with this version, SQLite automatically rebuilds the trade table
in one explicit transaction to replace the old unconditional daily constraint with
the partial index. Original JSON payloads, audit rows, and settings are preserved;
a migration failure rolls back and fails startup. Back up the database before
upgrading, using SQLite's backup API rather than copying a live WAL database.

### Arithmetic and fees

All financial arithmetic uses `Decimal`, in option **points** unless labeled
dollars. Dollar cash flows use `points × quantity × 100`.

```text
D = back_put_ask + back_call_ask - short_put_bid - short_call_bid
C = back_put_bid + back_call_bid - outer_put_ask - outer_call_ask
raw transform threshold = D + W
fee-aware threshold = D + W + (8 × commission_per_leg / 100)
raw floor points = C - D - W
raw tent points = C - D
net floor dollars = raw floor points × quantity × 100 - paid commissions
```

The default commission assumption is **$0.65 per contract per executed leg**;
entry has four and transform another four. The extra fee gate ensures a
nonnegative modeled floor **after those fees**. Set commission to zero to test
the exact original `C >= D + W` gate. Exchange/regulatory fees, slippage and
settlement charges beyond this fixed commission are not modeled. Configuration
changes affect new trades only.

### Bot-monitored calendar protection

New calendars use a **30% loss trigger**, evaluated by the bot on each successful
quote poll. **No resting, preset, or broker-side stop order is submitted.**
Full calendar liquidation credit is back-long bids minus front-short asks:

```text
L = back_put_bid + back_call_bid - short_put_ask - short_call_ask
loss percentage = (D - L) / D × 100
bot stop condition = L <= 0.70 × D
```

On that condition, the engine simulates selling both back longs and buying back
both front shorts at the observed natural quotes, then records all four closing
legs and `CLOSED_STOP_LOSS`. The threshold is measured against opening debit
**before commissions**; final P&L/ROI still deduct all paid commissions. Midpoint
quotes do not determine the loss trigger.

The same check runs **before entry**. If the proposed calendar would already
show a 30% or greater immediate liquidation loss from aggregate bid/ask costs,
the entry is rejected without creating a trade or simulated fills. The failed
attempt, six-leg quote snapshot, loss percentage and rejection reason are
persisted under `entry_rejection` and included in the ZIP report. Per-leg spread,
freshness, delta and displayed-size checks still apply.

The stop policy is captured per trade and displayed in the dashboard/audit.
Historical trades without this field retain their original **20%** policy,
including any still-open legacy calendars. Closed history and today's recorded
stop-out are not rewritten, reopened, or retried. New trades receive 30%.

Stops take priority over transforms on the same sample. Polling gaps, missing
data, disconnection and slippage can produce losses **greater than 30%**; this
is not a guaranteed loss cap. The bot must be running and receiving valid live
quotes. A transformed condor no longer uses the calendar loss trigger; its
existing expiration settlement workflow remains unchanged. The calendar exit
is a **sale credit**, not an “exit debit,” unless liquidation is negative.

**Untransformed expiration:** attempt full liquidation five minutes before the
front-session close (`CLOSED_CALENDAR_TIME_EXIT`) rather than silently dropping
the still-live back longs. This is an explicit additional safety rule. If data
is missing at that time, the position remains unresolved. An expired-calendar
recovery may use verified front settlement plus a current live sale of the
back longs in a later open session **before back-expiration close**, with actual sale
timestamps recorded. If both expirations were missed, no historical fills are
fabricated; manual cashflow reconciliation is required outside this version.

### Automatic SPXW expiration reconciliation

As selected for this study, **automatic settlement uses an explicitly labeled
IBKR closing-index proxy**, not a certified official exercise-settlement value.
After the front session close plus a **15-minute delay**, the app requests
historical **SPX / CBOE / TRADES / RTH / daily** bars and accepts only a finite,
positive closing value with the exact front-expiration date. Early closes use
their actual session close plus the same delay. Missing data leaves the
position pending and produces a visible error; retries are five minutes apart.
Expired untransformed calendars additionally wait for an open session to sell
their still-live back longs.

Automatic outcomes are marked `ibkr_close_proxy` in SQLite/audit JSON,
`PROXY` in CSV notes, and **IBKR PROXY** in completed-trade rows. The performance
widget counts proxy-settled outcomes explicitly. Treat these as modeled research
outcomes, not independently verified actual exercise cashflows.

Set `AUTO_SETTLE_FROM_IB_CLOSE=false` to require verified official input instead.
For any still-pending trade, the dashboard can record a verified **SPXW PM**
expiration value and source/statement reference, with confirmation. Never use
the AM `SET` value or an arbitrary intraday last tick. The source, provenance,
effective close timestamp and recording timestamp are persisted. Already closed
proxy outcomes are not silently overwritten by later official values; any
subsequent discrepancy requires external reconciliation of the exported study.

The condor is cash-settled at intrinsic values for all four remaining legs:

```text
spread loss = max(Kput − S, 0) − max(Kput − W − S, 0)
            + max(S − Kcall, 0) − max(S − Kcall − W, 0)
net P&L = (C − D − spread loss) × quantity × 100 − commissions
```

Between inner strikes, including boundaries: `CLOSED_MAX_PROFIT`.
At/beyond outer wings: `CLOSED_FLOOR`.
Between an inner strike and its wing: **`CLOSED_PARTIAL_PROFIT`**.
The third region is mathematically necessary and is not mislabeled max/floor.
No physical exercise/assignment or ETF pin risk is modeled.

## Dashboard and API

The dependency-light dashboard is a **same-origin static SPA** served by
FastAPI (plain JavaScript/CSS, no Node build or third-party CDN required).
It provides connection/market status, live countdown, persisted configuration,
position cards, fee-aware transform progress, stale-quote flags, automatic proxy
and manual verified settlement, realized statistics, streamed advisory/error feeds and CSV/ZIP
downloads. WebSocket updates every two seconds; broker polling is independent.

- `GET /api/state`, `GET /api/health`
- `PUT /api/config` — `symbol` must be `SPX`, quantity 1–100, wings 5 or 10
- `POST /api/entry` — JSON `{}`; manual live-data simulated entry
- `POST /api/entry/retest` — explicitly confirmed, UUID-idempotent extra test;
  see controlled manual retests above
- `POST /api/trades/{trade_id}/settlement` — JSON
  `{"value":"6000.00","source":"verified statement reference","confirmed_official_spxw_pm":true}`
- `GET /api/csv/trades_summary.csv`, `GET /api/csv/legs_execution_log.csv`
- `GET /api/report` — ZIP bundle including quote-evaluation, entry-rejection, manual-retest, advisor, error and
  settlement audit JSON plus current configuration
- `WS /api/stream` — dashboard state and feed updates

### Optional AI advisor

Disabled by default. Enable `ADVISOR_ENABLED=true` and configure the provider
in `.env`. Supported HTTP adapters: local **Ollama**, **OpenAI-compatible**
providers, and **Anthropic**. See the environment example for URLs.
Only public market context is sent, not account positions, credentials or code.
The prompt requests structured output containing two short sentences covering
front/back IV backwardation, VIX, bid/ask quality and the threshold. Responses
are validated for two sentences; malformed responses become visible errors.
Provider output is explanatory and is never trusted as a decision signal.

Entry and every calendar transform evaluation enqueue advisor work independently
of trading. A bounded queue and 12-second HTTP timeout prevent it from blocking
trade decisions. Provider failures and queue overflow are persisted and shown;
no fabricated “AI” response is emitted on failure. Disabled mode is labeled.

## Persistence and reporting

`data/paper.db` is the canonical state and audit journal (SQLite WAL).
Leg events and each trade state transition commit in a single transaction.
Do not edit the database while running. Back up the directory with the app
stopped so the WAL and database are consistent.

The two CSVs are projected under `data/csv/`:

1. `trades_summary.csv` — the requested 23 columns, **one current row per
   trade**, updated after transitions.
2. `legs_execution_log.csv` — the requested 14 columns, a growing audit of
   entry, transform, stop/time-exit and cash-settlement cashflows.

Both are deterministically regenerated from SQLite into fsynced temporary
files and individually atomically replaced. This intentionally avoids blind
CSV append: retries/restarts do not duplicate fills, and summary rows do not
retain obsolete states. A crash between the two replacements can leave one CSV
behind; startup regeneration repairs it. For a consistent pair, download the
locked ZIP report or stop the application before copying files.

Nulls are empty CSV cells; transformed is `TRUE/FALSE`; timestamps are ISO-8601
with offsets. Prices/floor/tent are per-contract points, capital and final P&L
are aggregate dollars. Settlement bid/ask/Greeks are blank because intrinsic
cash settlement is not an executed market quote. Extra lifecycle event/status
values above extend the requested schema without discarding edge cases.
The two CSV column schemas are unchanged. Each new trade's notes record its
bot-monitored loss threshold; rejection audits live in SQLite/report JSON,
not as fake trade/leg rows. Report configuration includes the current risk policy.
Quantities are joined from the summary by `trade_id`. Net final P&L/ROI deduct
modeled commissions; the original floor/tent columns preserve the raw formulas.

Transform rate uses all entered trades; profit factor/win rate use only closed
trades. No losses with positive wins gives infinite profit factor; no outcomes
gives N/A. Zero P&L is not counted as a win.

## Verification and synthetic samples

```bash
cd backend
../.venv/bin/pytest -q
../.venv/bin/ruff check app tests scripts
../.venv/bin/ruff format --check app tests scripts
../.venv/bin/pyright app
../.venv/bin/python -m scripts.generate_sample
```

The replay consumes [market_fixture.json](samples/market_fixture.json) and
uses the real paper engine to write isolated sample CSVs under
[samples/generated](samples/generated). It opens at **2.20 points**, transforms
42 minutes later for **7.30 points**, and settles in the tent for **$504.80 net**
with 12 leg events. These are **synthetic fixtures, not real market results**.
The generator never connects to IBKR and refuses the default production data
directory as an output target.

Tests cover exact fee-aware thresholds, 30% bot-stop/entry-guard boundaries,
legacy-policy preservation, quantity/cost
scaling, all payoff regions, session holidays/DST/half-days, duplicates,
restart/export idempotence, disconnects, quote validation, API validation and
same-origin writes, historical-date matching and automatic proxy reconciliation.
Live entitlements, real quote reception, broker pacing and
execution realism require your local TWS validation. Running tests successfully
is not evidence of profitability; two weeks is also too small to establish
reliable long-term strategy performance.

## Layout

Repository owner Git workflow preferences and agent guidance are documented in
[AGENTS.md](AGENTS.md). The owner permits direct commits and pushes to `main`
without requiring a PR for this repository.

```text
backend/app/core/          configuration, domain models, sessions, SQLite
backend/app/ibkr/          read-only ib_async adapter and chain selection
backend/app/engine/        Decimal arithmetic, state machine, scheduling, CSV
backend/app/advisor/       asynchronous optional provider adapters
backend/app/api/           endpoints and WebSocket state
backend/tests/            deterministic tests (no IBKR dependency)
backend/scripts/          isolated synthetic replay
frontend/                 dashboard, served without a frontend build
samples/                  labeled fixture and generated example CSVs
data/                     persistent runtime volume (ignored)
```

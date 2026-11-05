# CIP alerting backend

The logic from `denon_cip_alerting_clean.ipynb`, split into Python modules for the CIP Monitor backend.

## Start the UI

```bash
cd cip_backend
/opt/anaconda3/bin/python3 scripts/serve.py --open      # http://127.0.0.1:8000
```

The server loads the logs and processes all but the last 2 days instantly, so the page opens with history. It then plays the rest as live 60-second pings at 60× speed: one simulated minute per second. Use `--speed 600` to go faster, or `--start "2026-07-10 06:00"` to choose where live play begins. The page refreshes every 2 seconds.

The page has three working views in the top bar: **Overview** (alerts and the debug panel), **Run history** (`/#runs`) and **Audit log** (`/#audit`). "Objects" and "Recipes" are marked *Soon* and are not links.

It uses only Python's standard library for the web server. The installed FastAPI and Starlette versions don't work together.

## How it works

It runs in two ways, and both apply the same rules:

- **Batch**: reads the whole log files and gives the notebook's exact results.
- **Live**: takes one reading per object per minute, as an IoT device would send it, and raises each alert the minute a limit is crossed. For now a simulator replays the log files as those pings.

## Layout

```text
cip_backend/
├── cip_alerting/
│   ├── config.py       Settings: paths, tolerances, cycle gap, validation limits   (notebook §1)
│   ├── validation.py   ValidationLog: check(), ERROR stops, WARNING is recorded     (§1 helpers, §8)
│   ├── loader.py       Read CIP_Log*.xlsx: header row 14, line name, numeric clean  (§2)
│   ├── thresholds.py   Reference workbook, thresholds per object, equipment-code matching (§3)
│   ├── cycles.py       Split into cycles (gap > 30 min), minutes per activity       (§4)
│   ├── rules.py        Duration breach rule, flow monitoring, status, TACT mapping  (§5, walkthrough)
│   ├── alerts.py       Alert table plus the per-cycle debug view                     (§6, walkthrough)
│   ├── summaries.py    Cycles per object by status; cycles and alerts per day        (§7)
│   ├── checks.py       Every validation check from every stage                       (§2–§7)
│   ├── pipeline.py     run_batch(): the whole notebook as one call
│   ├── models.py       Reading (one ping), Alert, Event; JSON-ready to_dict()
│   ├── live.py         LiveMonitor: the same rules applied one reading at a time
│   ├── simulator.py    Replays log files as 60-second pings into the LiveMonitor
│   ├── store.py        SQLite: alerts as raised, append-only audit log (acknowledgements), run history
│   ├── ingest.py       Validation and JSON Schema of readings pushed by gateways (POST /api/readings)
│   ├── proposed_rules.py  Proposed T/A/C rules (pending Danone sign-off): live tracker + batch, CIP_PROPOSED_RULES
│   ├── notify.py       Teams webhook and email notifications from a background queue (all off unless configured)
│   └── api.py          ReplayService (replay or live mode) and the JSON HTTP API
├── rules/proposed_tact_rules.json   Proposed rule definitions (status "proposed"; thresholds, rationale, projection)
├── docs/TACT_rules_proposal.md      The proposal for Danone's process and quality engineers (evidence, open questions)
├── web/index.html      The page (v2, simplified): overview (summary cards, alert list, alert panel, run dialog),
│                       run history, audit log
├── scripts/
│   ├── serve.py           Starts the UI and the live replay
│   ├── run_batch.py       Summary, CSV export and the debug view of one cycle
│   ├── run_simulation.py  Live replay that prints alerts and cycles, plus overview JSON
│   └── push_readings.py   Device simulator: POSTs the log files to a live-mode server (the real IoT path)
└── tests/
    ├── test_parity.py     Checks against the notebook's numbers, and live against batch
    ├── test_api.py        Calls every API endpoint over HTTP
    ├── test_store.py      Store, audit log, CSV export; acknowledgements survive a restart and a looped replay
    ├── test_ingest.py     Reading validation, auth, ordering; all 38,943 readings over HTTP == batch
    ├── test_proposed_rules.py  Rule file, streak logic, modes, live == batch for proposed alerts, projection, API
    ├── test_notify.py     Teams payload (fake webhook), email (fake SMTP), throttle, routing, retries, warm-up
    └── ui/                Headless-Chrome tests of the page (cdp.py driver + test_e2e.py)
```

## Run

Run these commands from `cip_backend/`. The commands need pandas, numpy and openpyxl; the notebook's Anaconda base environment has them. Data is read from `../datasets/`, or from the folder given with `--data-dir`.

```bash
python scripts/run_batch.py --out output
python scripts/run_batch.py --walk "C02C01-PMT TANK-1" 14
python scripts/run_simulation.py --until "2026-07-05" --events cycles --overview
python scripts/run_simulation.py --speed 60          # one ping per second
python scripts/serve.py --open --loop                # UI on http://127.0.0.1:8000, replay restarts at the end
python -m pytest tests -q -p no:cacheprovider        # needs requirements-dev.txt and Google Chrome for tests/ui
```

```python
from cip_alerting import run_batch, LiveMonitor, Simulator, Settings, load_reference, load_readings

result = run_batch()                       # same numbers as the notebook
result.alerts; result.validation.issues(); result.walkthrough('C02C01-PMT TANK-1', 14)

settings = Settings()
monitor = LiveMonitor(load_reference(settings.reference_file), settings)
for event in monitor.ingest(reading):      # one Reading from one device
    ...                                     # alert_raised, cycle_started, cycle_closed, ...
monitor.overview()                          # data for the overview page
monitor.acknowledge(alert_id, by='operator')
monitor.trend(alert_id, 'Return Temp')      # trend line for the debug panel
```

## Verified

All 99 tests pass (about 5 min). `tests/ui/test_e2e.py` (31 tests, including the Lines page, its figures and the drill-down) drives the v2 page in headless Chrome at 1000–1920 px wide: summary numbers against every line × period, selection by mouse and keyboard, other alerts and notification links, one-click acknowledge (counts, audit log, reload, server restart), the run dialog, run history and audit log (filters, CSV), live updates, the loading, disconnected, replay-ended, live and stale states, and no clipped or overflowing text. It skips itself when Chrome or `websocket-client` is missing. The 8 tests in `test_parity.py` cover:

- **Batch matches the notebook**: 38,943 records, 699 cycles (329 OK, 211 NO REFERENCE, 159 ALERT), 221 breaches (112 Caustic, 64 Pre Rinse, 28 Final Rinse, 17 Acid) and the same 18 warnings.
- **Live matches batch**: replaying every reading gives the same 699 cycles (times, record counts, minutes per activity, median flows and status) and the same 221 alerts with the same final values.

## API

| Endpoint | Returns |
|---|---|
| `GET /api/status` | Replay state (`warming_up`, `live`, `finished`) and the clock |
| `GET /api/lines?period=24h\|7d\|all&days=14` | Manager view: plant totals and per line runs, runs within limits / over limit, planned and taken minutes, objects cleaned, runs in progress, alerts; plus a daily history (`days`, 1–60) per line |
| `GET /api/overview?period=24h\|7d\|all&line=M11W01` | Summary band and one row per object with its alerts |
| `GET /api/alerts/<id>[?others=all]` | Debug panel data: alert, TACT values, trends, cycle activity times, flows, data checks, earlier alerts, and `history` (this alert's audit trail, oldest first: raised, notifications, acknowledgement with who, when and note) |
| `POST /api/alerts/<id>/acknowledge` | Body `{"by": "name", "note": "optional"}`. `by` is required (1–64 characters; the page sends `operator`), `note` at most 280. Stored in the audit log; a repeat call returns the first acknowledgement |
| `GET /api/audit?limit=100&alert=<id>&line=<line>&object=<id>&event=<a,b>&facets=1` | Audit log, newest first: `alert_raised` (first time an alert is seen), `acknowledged` (actor, note), `notification_sent` / `notification_failed`, `service_started`, `replay_restarted`. Wall-clock (UTC) and plant time. Every filter is optional; `event` is a comma list (400 for a malformed name); `facets=1` adds the lines, objects and event names present (for the page's filters). `total` counts the filtered entries |
| `GET /api/audit.csv?<same filters>` | The audit log, or the filtered part, as CSV for quality teams, oldest first (UTF-8 with BOM for Excel; cells starting with `= + - @` are escaped). The page's **Export CSV** button links here with its current filters |
| `GET /api/runs?object=<id>&line=<line>&period=24h\|7d\|all&limit=100&summary=0` | Closed runs (run history), newest first. Each run has `durationMinutes`, `activities` (minutes, threshold, alert limit, breach), `flows` (median vs reference), `alerts` (those the monitor still holds) and `proposedAlertCount`; `summary=0` leaves out the full cycle summary row. `period` counts back from the monitor clock (default `all`; 400 if unknown). In replay mode runs after the replay clock are hidden |
| `GET /api/readings/schema` | JSON Schema of a reading upload |
| `POST /api/readings` | Live mode only (409 otherwise): readings from gateways, see below |
| `GET /api/proposed-rules` | The proposed T/A/C rules (status `proposed`), their mode and projected July alert counts |

`limit` is 1–1000 (400 otherwise). `GET /api/status` also has `mode` (`replay` or `live`), `store.persistent`, `proposedRules` (mode, rule count, whether they raise alerts) and `notifications` (which channels are configured, sent/failed counts, never a secret); in live mode `state` is `waiting`, `live` or `stale` (no reading for more than 3 min) with `lastReadingAt`, `lastReceivedAt` and `lastReadingAgeSeconds`.

Alert IDs contain `:` and spaces, so URL-encode them.

## The page (v2)

v2 keeps the same backend, API and data, with a calmer page. v1's fuller page is in git under the `v1.0` tag.

- **Lines** (the landing page, `/`): the manager view, always covering all four lines (the line selector is hidden here; the period selector applies). Five headline figures for the plant (runs within their time limits, CIP time taken against the planned reference time, objects cleaned, runs in progress, open alerts) and one card per CIP line: objects and whether all were cleaned, runs in progress, meters for runs within limits and CIP time against plan (the tick is the planned time; red when over), and the line's open alerts. **Inspect alerts →** (or a click on the card) opens the Alerts page filtered to that line. Below, the last 14 days (hover for figures): runs per day within / over limit; hours taken per day with a mark at the planned hours (red when over); and a line × day grid shaded by the share of runs within limits (darker is better, value in each cell).
- *Planned time* is the sum of the reference times of the timed activities (pre-rinse, caustic, acid, final rinse) a run went through; *taken* is the minutes those same activities took. Runs without reference times count as runs but not towards time or "within limits".

- **Alerts** (`/#alerts`, the former overview): three summary cards and one alert list. **Alerts** (with the acknowledged count beside it; the list header shows how many are open) is the default view. **Objects with alerts · N of 29** and **CIP runs in progress** filter the list: all objects including those without alerts, or only objects being cleaned now (click again, or *Show all alerts*, to clear). The panel keeps its alert while filtering. Each object with alerts is one row: its newest open alert, time against the limit, and "+N more". Rows group into *Needs attention* and *Acknowledged*; objects without alerts collapse into one "✓ N objects without alerts" line that expands.
- **Alert panel**, visual rather than wordy: the actual minutes with a "+N min over" chip and a bar (blue up to the limit, red beyond) with the limit and the time it was crossed (the full sentence is in the tooltip and for screen readers); the 6 CIP steps with their minutes (red = over its limit, striped = running, dashed = not run); the temperature, flow and conductivity used in that step as gauges with a set-point tick (an amber "Invalid" chip when outside the plausible sensor range; parameters not used in the step are left out); data checks as chips (e.g. "Supply flow · 43 invalid"); and the object's other alerts: the latest 4, with **+N earlier** to load the full list (`GET /api/alerts/<id>?others=all`) and *Show fewer* to collapse it. **Open run** shows each step against its limit and the flow medians against the reference.
- **Acknowledge is one click.** It is recorded in the audit log as "Operator". The API still accepts a name and a note (`POST /api/alerts/<id>/acknowledge`).
- **Run history** (`/#runs`) and **Audit log** (`/#audit`, filters by object and event, **Export CSV** of that selection) follow the line and period selectors.
- **Not shown on the page for now** (still in the backend and API): the proposed T/A/C limits (`/api/proposed-rules`, `CIP_PROPOSED_RULES`), the per-alert audit trail (`GET /api/alerts/<id>` → `history`) and the notification status (`/api/status` → `notifications`).
- **Data-driven deviations from the mock-up** stay as before: open/acknowledged instead of *Critical*, because no severity rule exists; only Time raises alerts; flow in L/h and conductivity in mS/cm; 6 CIP steps; the line selector picks the CIP line within the Ambala plant.
- **Status indicator**: "Catching up" during warm-up, "Live · date time" while replaying, "Replay ended" at the end of the data, "Disconnected" in red with a banner if the API stops responding. In live mode: "Waiting for readings…", "Live · last reading N s ago", and "No data for N min" in amber with a banner when no reading arrived for longer than `CIP_STALE_AFTER_SECONDS`.
- **Links from notifications**: `/#alert=<id>` opens that alert, even when it is outside the selected period.
- **Screen sizes**: 1280–1920 px side by side; below 1180 px the panel moves under the list.

## How the live monitor works

For each `(line, object)`:

1. **New cycle**: a reading more than `cycle_gap_minutes` (30) after the previous one closes the old cycle and starts a new one. `close_idle(now)` closes silent objects without waiting for their next reading.
2. **Activity time**: last `Time In Seconds` minus first, per activity in the cycle. This is the batch formula, updated with each reading.
3. **Alert**: raised on the first reading where activity time > threshold × 1.10. It holds the TACT values at that moment. Its `actual_minutes` keeps growing until the cycle closes.
4. **Data quality**: the notebook's record-level checks run on each reading: unknown recipe, activity or step; sensor out of range; negative counter; counter restart; repeated activity. Each issue is reported once per cycle.
5. **Flow**: checked against the reference when the cycle closes, for monitoring only. It never creates an alert, as in the notebook.

## Persistence and audit trail

`cip_alerting/store.py` keeps, in one SQLite file (`CIP_DB_PATH`, default `cip_backend/state/cip_monitor.db`; `/state/cip_monitor.db` in Docker):

- **alerts**: every alert as raised (id, object, cycle, activity, threshold / limit / actual minutes, `raised_at` plant time, `created_at` wall time); final minutes are filled in when the cycle closes.
- **audit_log**: append-only (SQLite triggers refuse UPDATE and DELETE). One `acknowledged` row per alert (a unique index), with who, the optional note, wall-clock and plant time.
- **runs**: every closed cycle with its full summary (run history).

An acknowledgement belongs to an alert's id **and** its `raised_at`. A replay (restart or `--loop`) raises the same alert at the same plant time, so it comes back acknowledged with the original name and time. In live mode cycle numbers restart at 0 after a restart, so a new alert can reuse an old id; its `raised_at` differs, so it is never matched to the old acknowledgement.

Writes from the monitor are buffered and written in one transaction (the warm-up writes ~220 alerts and ~700 runs: about 0.05 s on top of the 0.8 s replay). Acknowledgements are on disk (WAL, `synchronous=FULL`) before the API answers. One connection is shared behind a lock. If the file cannot be opened, the server logs a loud warning and keeps everything in memory (`store.persistent` is false in `/api/status`).

## Live mode: readings from gateways

`CIP_MODE=live` (or `--mode live`) starts the server without a replay. Gateways push readings to `POST /api/readings`; the clock is the latest reading. The feed counts as stale after `CIP_STALE_AFTER_SECONDS` (default 180; `--stale-after`) without a reading. Set `CIP_INGEST_TOKEN` to require `Authorization: Bearer <token>` (compared in constant time, 401 otherwise); without it the server logs a warning, because anyone who can reach the port can push data.

The body is one reading or `{"readings": [...]}` (at most 5,000; `Content-Type: application/json`). Field names are the **CIP log column names**, so a gateway can forward the PLC tags unchanged:

```json
{"readings": [{"Date Time": "2026-07-24T09:50:12", "Line": "M11W01", "Object": "C02C01-PMT TANK-1",
               "Recipe": "Caustic", "Activity": "Caustic", "Step": "Circulation", "Time In Seconds": 1260,
               "Temp SP": 75, "Return Temp": 74.6, "Supply Flow": 18950, "Return Flow": 18210,
               "Min Return Flow SP": 16500, "Return Conducivity": 92.4, "Caustic SP": 90, "Acid SP": null,
               "Final Rinse Cond. SP": null, "Flip": 0}]}
```

- Required: `Date Time` (ISO 8601 plant local time without a UTC offset), `Line`, `Object`. Everything else may be missing or `null`, like an empty log cell; unknown recipe, activity or step names are accepted and reported as data checks.
- Numbers must be finite JSON numbers. Unknown field names are refused (so `Return Conductivity` for the PLC's `Return Conducivity` is caught). `GET /api/readings/schema` returns the JSON Schema.
- Any invalid field: **400**, nothing is stored, and `errors` lists each problem as `{"index", "field", "message"}`.
- Readings of one object must arrive in time order. A reading at the same time as, or earlier than, the object's last accepted reading is rejected with reason `duplicate` or `out_of_order` (as is one more than a day ahead of the server clock: `in_future`); the other readings are accepted. Retrying a batch is therefore safe.
- Response: `{"accepted", "rejected", "rejections": [...], "events": {"alert_raised": 1, ...}, "alertsRaised": [...], "clock"}`.
- A cycle closes when the object's next reading comes more than 30 minutes later, or when the plant clock (latest reading, plus wall-clock time since it arrived) is more than 30 minutes + one reading interval past the object's last reading.
- A restart starts with an empty monitor: alerts of runs before the restart are in `/api/audit` and `/api/runs`, not on the overview.

One reading with curl:

```bash
curl -sS -X POST http://127.0.0.1:8080/api/readings \
  -H 'Content-Type: application/json' -H 'Authorization: Bearer secret' \
  -d '{"Date Time": "2026-07-24T09:50:12", "Line": "M11W01", "Object": "C02C01-PMT TANK-1",
       "Recipe": "Caustic", "Activity": "Caustic", "Step": "Circulation", "Time In Seconds": 1260,
       "Temp SP": 75, "Return Temp": 74.6, "Return Flow": 18210, "Min Return Flow SP": 16500,
       "Return Conducivity": 92.4, "Caustic SP": 90}'
# {"accepted": 1, "rejected": 0, "rejections": [], "events": {...}, "alertsRaised": [], "clock": "2026-07-24T09:50:12"}
```

Try it end to end with the device simulator, which reads the log files and posts them:

```bash
CIP_MODE=live CIP_INGEST_TOKEN=secret python scripts/serve.py --port 8080
CIP_INGEST_TOKEN=secret python scripts/push_readings.py --url http://127.0.0.1:8080 --speed 600   # 10 min/s
python scripts/push_readings.py --url http://127.0.0.1:8080 --token secret --speed 0 --batch-size 5000  # all in ~1 s
```

`tests/test_ingest.py` pushes all 38,943 readings this way and checks the same 221 alerts and 699 cycles as the batch pipeline.

## Mapping to the overview page spec

| UI element | Backend | Notes |
|---|---|---|
| Open alerts / Objects with alerts / Runs in progress | `monitor.overview()['summary']` | Calculated from data, not hard-coded |
| **Critical** count, severity dot | `Alert.severity` = `None` | The notebook has no severity rule (spec open question 5) |
| Alert row: title, step, raised time | `Alert.title`, `step_at_raise`, `raised_at` | |
| Temperature / Action / Concentration / Time | `Alert.tact_at_raise` (`rules.tact_panel`) | Return Temp vs Temp SP; Return Flow vs Min Return Flow SP; Return Conducivity vs Caustic/Acid SP; activity minutes vs threshold. T/A/C entries carry `proposed` (the proposed rule's verdict) unless `CIP_PROPOSED_RULES=off` |
| "Why it was raised" | `Alert.explanation`, `Alert.rule` | |
| Trend lines | `monitor.trend(alert_id, column)` | |
| CIP step bar | `config.ACTIVITIES` | The logs have 6 activities (including Final Drain), not 5 |
| "Check first" | — | Not in the notebook; needs input from Danone |
| Acknowledge | `ReplayService.acknowledge()` → `store.py` | Append-only audit log in SQLite; survives restarts and looped replays |
| "No data" sensor state | TACT status `no_data` | Never shown as within limit |

## Differences from the UI mock-up

- **Only Time raises alerts by default.** The notebook alerts when an activity runs *too long*. Temperature, flow and conductivity keep the status `not_evaluated`, because no approved limit rule exists for them; the proposed rules below are shown next to them (or, with `CIP_PROPOSED_RULES=alert`, raise alerts marked as proposed).
- **No knock-on status.** The spec's knock-on rule covers a Time reading that is *short* because another parameter failed. The notebook's rule is the opposite case (Time too long), so the backend does not set `knock_on`. The process team needs to define it.
- **Units differ from the mock-up.** Flow is in L/hr from the PLC, not m/s or bar, and concentration is conductivity in mS/cm, not %.

## Open points carried over from the notebook

1. The **Acid threshold** includes Mid Rinse, but it is compared with Acid alone (17 alerts now, 31 if Mid Rinse is added).
2. **Cycles** split only on a 30-minute gap. 53 cycles contain a counter restart, which means back-to-back runs were merged. The live monitor flags these as `counter_restart`.
3. **Flow** uses the median over the whole cycle, which includes pump-off readings.
4. **Object mapping**: 7 log objects (RMT Tank-1/2/3, RMT To Reception, Milk Reception, Raw Milk Tanker, Milk Past-2) have workbook rows with no usable equipment code, so the notebook's code match leaves them at NO REFERENCE (211 cycles). `rules/object_aliases.json` maps them by name (used only when the code match fails) and is **on by default**: all 699 cycles are then monitored and the Time rule raises **561 alerts** instead of 221 (live still equals batch, `tests/test_aliases.py`). `CIP_OBJECT_ALIASES=off` restores the notebook's numbers; the other test suites run in that mode. **To confirm with Danone:** (a) Milk Past-2's workbook row carries code C10W02, assumed to be a typo for C01W02; (b) Raw Milk Tanker overruns on 51 of 52 runs in every activity (final rinse median 13.1 min vs 5.3 min limit) and its PLC Min Return Flow SP differs from the workbook, so that row may not be the right reference (153 of the 340 extra alerts). Remove its line from the alias file to stop monitoring it.
5. The **pre-rinse threshold** is the proposed timer, applied from 19 Aug 2026, after the July log period.
6. The **10% tolerances** are working defaults, not agreed plant rules.

## Proposed T/A/C rules (pending Danone sign-off)

[`docs/TACT_rules_proposal.md`](docs/TACT_rules_proposal.md) proposes one rule per TACT parameter and activity, derived from the July 2026 logs, with the evidence, sensitivity and open questions for Danone. The rules are in `rules/proposed_tact_rules.json` (every rule has `status: "proposed"`; a file with another status is refused) and run in `cip_alerting/proposed_rules.py`:

> *signal* below *limit* (set point − X °C, or Y% of set point) for N minutes or longer during the activity's circulation, ignoring the first G minutes (ramp-up). One alert per rule per cycle; objects with reference thresholds only.

| Rule | Limit | Projected alerts, July |
|---|---|---:|
| Temperature, caustic and acid | > 5 °C below SP for ≥ 5 min (first 5 min not checked) | 7 + 4 |
| Concentration, caustic and acid | < 90% of SP for ≥ 5 min (first 5 min not checked) | 5 + 5 |
| Flow, caustic and acid | < 90% of Min Return Flow SP for ≥ 5 min (first 5 min not checked) | 14 + 0 |
| Flow, pre / mid / final rinse | < 90% of Min Return Flow SP for ≥ 3 min (first 2 min not checked) | 5 + 1 + 6 |

`CIP_PROPOSED_RULES` (or `--proposed-rules`):

- `show` (default): the debug panel shows the proposed verdict; **no alert is raised**, so the counts stay the notebook's (221 alerts, 699 cycles).
- `alert`: the rules also raise alerts (`proposed: true`, `parameter` T/A/C, `ruleId`, `measured`), stored, audited and notified like Time alerts. Adds 47 alerts over the July data. Only for trials with Danone.
- `off`: not loaded.

The live monitor (`ProposedTracker`, one reading at a time) and the batch pipeline (`evaluate_batch`, pandas; `BatchResult.proposed_alerts`) implement the rule independently; `tests/test_proposed_rules.py` checks they raise the same 47 alerts at the same minute, and that the counts quoted in the JSON still match the data. `python scripts/run_batch.py` prints the projection; `--out` also writes `proposed_alerts.csv`.

## Notifications (Microsoft Teams and email)

`cip_alerting/notify.py`. Everything is off unless configured:

| Variable | Meaning |
|---|---|
| `CIP_TEAMS_WEBHOOK` | Teams incoming webhook (or Workflows "post to a channel" webhook) URL. Posts an Adaptive Card: object, title, actual vs limit, raised time (plant time), cycle, and an "Open in CIP Monitor" button |
| `CIP_PUBLIC_URL` | Base URL of the page, for the link (`<url>/#alert=<id>`) |
| `CIP_SMTP_HOST`, `CIP_SMTP_PORT` (587), `CIP_SMTP_USER`, `CIP_SMTP_PASSWORD`, `CIP_SMTP_FROM`, `CIP_SMTP_TO` (comma list), `CIP_SMTP_STARTTLS` (1) | Plain-text email; on when host, from and to are set. Port 465 uses implicit TLS |
| `CIP_NOTIFY_LINES` | Optional comma list of CIP lines to notify (others are skipped) |
| `CIP_NOTIFY_MIN_INTERVAL` | Seconds between messages for one object (default 300); alerts in between are counted as throttled |
| `CIP_NOTIFY_IN_REPLAY` | `1` = also notify in replay (demo) mode. Default off: a replay never notifies |
| `CIP_NOTIFY_MAX_LAG_MINUTES` | Live mode: alerts more than this many plant minutes behind the newest reading (a gateway uploading a backlog) are not notified (default 10) |

- Only alerts raised **while live** are sent: never during the replay's fast-forward warm-up, never twice for the same alert (a looped replay or restart raises it again; the store knows it), never for one already acknowledged.
- Messages go through a bounded queue to one background thread, so the replay or ingest thread never waits on the network. Each channel is retried with exponential backoff (2, 4, 8 s); a 4xx from the webhook or a refused SMTP login is not retried.
- Every outcome is in the audit log: `notification_sent` / `notification_failed` (channel, attempts, error). The webhook URL and SMTP password never appear in logs, the audit log or `/api/status`.
- Proposed-rule alerts say "Proposed — pending Danone sign-off" in the card and email.

## Docker

The demo runs as one always-on container (`cip-monitor`). Everything below is run from the project folder (the one that holds `datasets/` and `docker-compose.yml`).

```bash
docker compose up -d --build        # build the image and start in the background -> http://localhost:8000
docker compose ps                   # STATUS shows "(healthy)" about 5-15 s after start
docker compose logs -f cip-monitor  # follow the server log
docker compose restart              # replay again from the start
docker compose down                 # stop and remove the container
```

**Where the data comes from.** The client's log files are never copied into the image. `./datasets` is mounted read-only at `/data` in the container. It must hold the `CIP_Log*.xlsx` files and `CIP Pre Rinse Timer details.xlsx`. To use a different folder, change the `volumes:` line in `docker-compose.yml`. On a Linux host the files must be readable by the container user (uid 10001): `chmod -R a+rX datasets`.

**Settings** (environment variables). Copy `.env.example` (project folder, safe defaults, no secrets) to `.env` next to `docker-compose.yml` and edit it, or put them before the command. Keep comments in `.env` on their own lines. Never commit a `.env` with real tokens or passwords (`.gitignore` excludes it).

| Variable | Default | Meaning |
|---|---|---|
| `CIP_PORT` | `8000` | Port on the host |
| `CIP_SPEED` | `60` | x real time; `60` = one simulated minute per second, `600` = ten |
| `CIP_START` | empty | Replay instantly up to this time, then play live, e.g. `2026-07-24 06:00`. Empty = 2 days before the end of the logs |
| `CIP_MODE` | `replay` | `replay` plays the log files; `live` waits for gateways to `POST /api/readings` |
| `CIP_INGEST_TOKEN` | empty | Live mode: bearer token required on `POST /api/readings` (set it whenever the port is reachable by others) |
| `CIP_STALE_AFTER_SECONDS` | `180` | Live mode: seconds without a reading before the page shows "Stale" |
| `CIP_OBJECT_ALIASES` | on | `off` = equipment-code matching only (the notebook's 221 alerts); a path = another alias file. Default: `rules/object_aliases.json` (7 objects matched by name) |
| `CIP_PROPOSED_RULES` | `show` | `off`, `show` (debug panel only) or `alert` (proposed T/A/C rules also raise alerts; pending Danone sign-off) |
| `CIP_TEAMS_WEBHOOK`, `CIP_SMTP_*`, `CIP_PUBLIC_URL`, `CIP_NOTIFY_*` | empty (port 587, STARTTLS on, interval 300 s, max lag 10 min) | Notifications, see above. Off unless set; in replay mode only with `CIP_NOTIFY_IN_REPLAY=1` |
| `CIP_DB_PATH` | `/state/cip_monitor.db` | SQLite file for acknowledgements, audit log and run history. Compose mounts the named volume `cip-state` at `/state`, so it survives `docker compose down`/`up` and image rebuilds (`docker compose down -v` deletes it) |

```bash
CIP_SPEED=600 docker compose up -d                 # faster demo (re-creates the container)
CIP_PORT=8090 docker compose up -d                 # host port 8090
```

The container restarts itself after a crash or a Docker restart (`restart: unless-stopped`) and replays the logs in a loop, so the page always has live data. Its root file system is read-only (`read_only: true`, `/tmp` on tmpfs); the only writable place is the `cip-state` volume at `/state`. Acknowledgements, notes and the audit log survive `docker compose restart`, `down`/`up` and image rebuilds. After you edit the code or `web/index.html`, run `docker compose up -d --build` again.

**Packaging.** To hand the app over without the source: `docker save cip-monitor:latest | gzip > cip-monitor.tar.gz`, then on the target machine `docker load -i cip-monitor.tar.gz` and `docker compose up -d` with the same `docker-compose.yml` and a `datasets/` folder.

**Without Compose:**

```bash
docker build -t cip-monitor ./cip_backend
docker run -d --name cip-monitor -p 8000:8000 -v "$PWD/datasets:/data:ro" -v cip-state:/state \
  --read-only --tmpfs /tmp --env-file .env --restart unless-stopped cip-monitor
```

The image is `python:3.12-slim` with pandas, numpy and openpyxl only, runs as the non-root user `cip`, and has a health check on `/api/status`. Dev and test dependencies are in `requirements-dev.txt` (`pip install -r requirements-dev.txt`).

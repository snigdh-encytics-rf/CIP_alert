# CIP Monitor

CIP (Clean-in-Place) monitoring for Danone's Ambala plant. It reads the CIP log files, compares every cleaning run with the reference times in the pre-rinse timer workbook, raises an alert when a step runs more than 10% over its limit, and shows it all in a web page:

- **Lines**: the manager view. Plant figures, one card per CIP line, and the last 14 days as charts. Click a day to see what happened on it.
- **Alerts**: every alert with what went wrong, the CIP steps, the readings at the time, and an Acknowledge button.
- **Run history** and **Audit log**: every completed run, and every alert raised and acknowledged. The audit log exports to CSV.

Until the plant's gateways send live data, the monitor replays the July 2026 log files as if they were arriving live, one reading per minute per object.

---

## What you need

| | Docker (recommended) | Without Docker |
|---|---|---|
| Software | [Docker Desktop](https://www.docker.com/products/docker-desktop/) on Windows or macOS, or Docker Engine with the Compose plugin on Linux | Python **3.10, 3.11 or 3.12** (not 3.13: the pinned pandas and numpy don't support it yet) |
| Disk | about 1 GB for the image | about 300 MB for the Python packages |
| Both | Git (or download the repository as a ZIP from GitHub), and the data files below | |

### The data files

The data is the client's and is **not in this repository**. Get these files from the project team:

| File | What it is |
|---|---|
| `CIP_Log-<date>.xlsx` (one or more) | CIP log exports from the plant. Any file whose name starts with `CIP_Log` and ends in `.xlsx` is read. |
| `CIP Pre Rinse Timer details.xlsx` | The reference workbook with the time limits. It must have exactly this name. |

---

## Set up and start (Docker)

**1. Get the code**

```bash
git clone https://github.com/snigdh-encytics-rf/CIP_alert.git
cd CIP_alert
```

**2. Add the data.** Put the files in the `datasets` folder inside the project:

```text
CIP_alert/
├── datasets/
│   ├── CIP_Log-202607261035.xlsx
│   ├── CIP_Log-202607261035 (1).xlsx
│   └── CIP Pre Rinse Timer details.xlsx
├── docker-compose.yml
└── ...
```

On Linux only, let the container read them: `chmod -R a+rX datasets`

**3. Start it.** Make sure Docker Desktop is running, then, from the `CIP_alert` folder:

```bash
docker compose up -d --build
```

The first build downloads Python and the libraries, which takes a minute or two. After that, starting takes about 15 seconds.

**4. Open http://localhost:8000**

`docker compose ps` shows `(healthy)` once it is ready. The container restarts by itself after a reboot or a crash, and replays the logs in a loop, so the page always has live data.

### Everyday commands

Run these from the project folder.

| To... | Run |
|---|---|
| Stop it | `docker compose stop` |
| Start it again | `docker compose start` |
| See what it is doing | `docker compose logs -f` (Ctrl+C to stop following) |
| Get a newer version | `git pull`, then `docker compose up -d --build` |
| Remove it, keeping acknowledgements and the audit log | `docker compose down` |
| Remove it and wipe acknowledgements and the audit log | `docker compose down -v` |

### Settings

The defaults work as they are. To change something, copy `.env.example` to `.env` in the same folder, edit it, and run `docker compose up -d` again. The settings you are most likely to need:

| Setting | Default | What it does |
|---|---|---|
| `CIP_PORT` | `8000` | Port the page is served on: http://localhost:8000 |
| `CIP_SPEED` | `60` | Replay speed. `60` plays one plant minute per second; `600` is ten times faster. |
| `CIP_MODE` | `replay` | `live` stops the replay and waits for plant gateways to send readings to `POST /api/readings`. Set `CIP_INGEST_TOKEN` as well. |
| `CIP_TEAMS_WEBHOOK`, `CIP_SMTP_*` | empty | Send new alerts to Microsoft Teams or email. Off until set. |

Every setting is listed, with comments, in [`.env.example`](.env.example) and explained in [`cip_backend/README.md`](cip_backend/README.md#docker). Never commit a `.env` that holds passwords or tokens; `.gitignore` already excludes it.

---

## Set up and start (without Docker)

Steps 1 and 2 are the same as above. Then, from the project folder:

macOS / Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r cip_backend/requirements.txt
python cip_backend/scripts/serve.py --open
```

Windows (PowerShell):

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r cip_backend\requirements.txt
python cip_backend\scripts\serve.py --open
```

`--open` opens http://127.0.0.1:8000 in your browser. Press Ctrl+C to stop. Useful options:

| Option | Meaning |
|---|---|
| `--port 8090` | Use another port |
| `--speed 600` | Replay ten times faster |
| `--loop` | Start the replay again when the logs run out (Docker does this by default) |
| `--host 0.0.0.0` | Let other computers on the network open the page (by default only this computer can) |

Acknowledgements and the audit log are kept in `cip_backend/state/cip_monitor.db`.

---

## Troubleshooting

| You see | Do this |
|---|---|
| `Cannot connect to the Docker daemon` / `error during connect` | Start Docker Desktop and wait until it says it is running. |
| `port is already allocated` / `address already in use` | Something else uses port 8000. Run `CIP_PORT=8090 docker compose up -d` (or set `CIP_PORT` in `.env`), or `--port 8090` without Docker, and open that port. |
| `docker compose ps` shows `restarting` or `unhealthy` | Check `docker compose logs`. `Reference workbook not found` or `No CIP log files given` means the data files are missing, misnamed or not in `datasets/`. |
| `Permission denied` reading `/data` (Linux) | `chmod -R a+rX datasets` |
| `pip install` fails building numpy or pandas | Your Python is too new. Use Python 3.12, e.g. `py -3.12` on Windows or `python3.12` on macOS/Linux. |
| The page says **Disconnected** | The server stopped. Start it again (`docker compose start`, or rerun `serve.py`). The page reconnects by itself. |
| The page loads but shows no data | Wait about 15 seconds after starting: the logs are loaded first. |

---

## For developers

```bash
pip install -r cip_backend/requirements-dev.txt
python -m pytest cip_backend/tests -q -p no:cacheprovider
```

That runs about 100 tests in about 5 minutes. The data tests need the files in `datasets/`. The browser tests need Google Chrome, and skip themselves without it. How the backend works, the API, live mode, notifications and the proposed temperature/flow/concentration rules are all in [`cip_backend/README.md`](cip_backend/README.md).

| Path | What it is |
|---|---|
| [`cip_backend/`](cip_backend/) | The product: the alerting logic (`cip_alerting/`), the web page (`web/index.html`), scripts and tests |
| [`docker-compose.yml`](docker-compose.yml), [`cip_backend/Dockerfile`](cip_backend/Dockerfile) | How the container is built and run |
| [`.env.example`](.env.example) | Every setting, with comments |
| [`cip_backend/rules/object_aliases.json`](cip_backend/rules/object_aliases.json) | Maps 7 objects to workbook rows that have no equipment code (to be confirmed with Danone) |
| [`cip_backend/docs/TACT_rules_proposal.md`](cip_backend/docs/TACT_rules_proposal.md) | Proposed temperature, flow and concentration rules, pending Danone sign-off |
| [`denon_cip_alerting_clean.ipynb`](denon_cip_alerting_clean.ipynb) | The analysis notebook the logic comes from. Its outputs are stripped when committed, because they contain client data. |

Versions are tagged in git (`v1.0`, `v2.0`, `v3.0`, ...). For example, `git checkout v2.0` returns to an earlier version.

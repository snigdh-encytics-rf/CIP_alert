# CIP Monitor

CIP alerting for Danone's Ambala plant. It compares each CIP run in the CIP log files with the expected durations and flows in the pre-rinse timer workbook, and raises an alert when an activity runs more than 10% over its threshold.

## Contents

| Path | What it is |
|---|---|
| [`cip_backend/`](cip_backend/) | The product: the alerting backend, a live monitor fed by 60-second readings, the web UI and the tests. See [`cip_backend/README.md`](cip_backend/README.md). |
| [`docker-compose.yml`](docker-compose.yml) | Runs the monitor in Docker. |
| [`.env.example`](.env.example) | Every setting, with comments. |
| [`denon_cip_alerting_clean.ipynb`](denon_cip_alerting_clean.ipynb) | The analysis notebook the backend logic comes from. Its outputs are stripped on commit. |
| [`cip_backend/docs/TACT_rules_proposal.md`](cip_backend/docs/TACT_rules_proposal.md) | Proposed temperature, flow and concentration rules, pending Danone sign-off. |

## Quick start

1. Put the data in `datasets/` (see below).
2. From this folder, run:

   ```bash
   docker compose up -d --build
   ```

3. Open http://localhost:8000.

To run without Docker, see [`cip_backend/README.md`](cip_backend/README.md).

## Data

The datasets are client data and are not in this repository; `.gitignore` excludes them. Put these files in `datasets/`:

```text
datasets/
├── CIP_Log-<timestamp>.xlsx          one or more CIP log workbooks
└── CIP Pre Rinse Timer details.xlsx  reference thresholds (Summary and Process CIP Water Consumption sheets)
```

Docker mounts them read-only. They are never copied into the image.

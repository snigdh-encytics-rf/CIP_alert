# CIP Alerting

This project compares actual CIP cycles from the CIP log files with the expected timing and flow thresholds from the pre-rinse timer workbook.

## Main notebook

Start with [`denon_cip_alerting.ipynb`](denon_cip_alerting.ipynb). This is the main notebook for building and reviewing the CIP threshold alerting system.

The other notebooks are supporting analysis and data-story notebooks.

## Required files

Put the following files in the `datasets/` folder before running the notebooks:

- One or more CIP log workbooks named like `CIP_Log*.xlsx`.
- `CIP Pre Rinse Timer details.xlsx`.

The timer workbook should contain:

- A `Summary` sheet with the proposed pre-rinse, caustic, acid, and flow set points.
- A `Process CIP Water Consumption` sheet used to calculate the final-rinse total from the four final-rinse steps.

Datasets are intentionally excluded from GitHub. Keep the folder structure like this:

```text
datasets/
├── CIP_Log-<timestamp>.xlsx
└── CIP Pre Rinse Timer details.xlsx
```

After adding the files, open `denon_cip_alerting.ipynb`, run the cells from top to bottom, and review the cycles flagged as outside their thresholds.

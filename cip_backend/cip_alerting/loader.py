"""Load CIP station log workbooks (notebook section 2).

Each workbook has a report header (line name, date range), column names on
Excel row 14, a units row, then one record per minute.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

import pandas as pd

REQUIRED_COLUMNS = [
    'Date Time', 'Object', 'Recipe', 'Activity', 'Step',
    'Time In Seconds', 'Temp SP', 'Return Temp', 'Supply Flow',
    'Return Flow', 'Min Return Flow SP', 'Return Conducivity',
    'Caustic SP', 'Acid SP', 'Final Rinse Cond. SP', 'Flip',
]
TEXT_COLUMNS = REQUIRED_COLUMNS[1:5]
NUMERIC_COLUMNS = REQUIRED_COLUMNS[5:]
HEADER_ROW = 13  # Excel row 14 (0-based index)
LINE_PATTERN = re.compile(r'Line\s*:\s*([^ ]+)', flags=re.I)


def read_log(path: Path) -> tuple[pd.DataFrame, dict]:
    """Read one log workbook. Returns the records and load statistics for the checks."""
    path = Path(path)
    raw = pd.read_excel(path, sheet_name=0, header=None)
    header_text = ' '.join(raw.head(HEADER_ROW).astype(str).stack())
    line_match = LINE_PATTERN.search(header_text)
    line = line_match.group(1) if line_match else path.stem

    df = raw.iloc[HEADER_ROW + 1:].set_axis(raw.iloc[HEADER_ROW], axis=1)
    missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]
    if missing:
        raise ValueError(f'{path.name}: header row {HEADER_ROW + 1} is missing columns {missing}')
    df = df[REQUIRED_COLUMNS].copy()
    rows_read = len(df)

    df['Date Time'] = pd.to_datetime(df['Date Time'], errors='coerce')
    df = df[df['Date Time'].notna()].copy()  # drops the units row and blank rows
    values_lost = 0
    for col in NUMERIC_COLUMNS:
        had_value = df[col].notna()
        df[col] = pd.to_numeric(df[col], errors='coerce')
        values_lost += int((had_value & df[col].isna()).sum())

    df['Line'] = line
    df['Source File'] = path.name
    df['Excel Row'] = df.index + 1  # row in the source workbook, for tracing values back
    stats = {'Source File': path.name, 'Line': line, 'Line From Header': bool(line_match),
             'Rows Read': rows_read, 'Rows Without Timestamp': rows_read - len(df),
             'Values Lost In Numeric Conversion': values_lost}
    return df, stats


def load_logs(paths: Iterable[Path]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Read and combine every log file. Returns (logs, load_report)."""
    paths = list(paths)
    if not paths:
        raise FileNotFoundError('No CIP log files given')
    frames, stats = zip(*(read_log(path) for path in paths))
    logs = pd.concat(frames, ignore_index=True)
    logs = logs.sort_values(['Line', 'Object', 'Date Time']).reset_index(drop=True)
    return logs, pd.DataFrame(list(stats))

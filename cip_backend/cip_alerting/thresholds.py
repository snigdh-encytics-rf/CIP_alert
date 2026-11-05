"""Thresholds per object and log-object matching (notebook section 3).

| Threshold             | Source                                                          |
|-----------------------|-----------------------------------------------------------------|
| Pre Rinse             | Summary: ``New Proposed Pre Rinse total time(Min)``             |
| Caustic, Acid         | Summary: ``Caustic- Total Time``, ``Acid Total Time``           |
| Final Rinse           | Process sheet: sum of the 4 final-rinse steps (seconds / 60)    |
| Supply / Return flow  | Process sheet: ``Suppy Flow (L/Hr)``, ``Return Flow (L/Hr)``    |

Log objects are matched to workbook objects by their equipment code,
e.g. ``C02C01-PMT TANK-1`` -> ``M11W01_03_C02C01_PMT-1``. Workbook rows without a usable code
(``RMT1``, ``Milk Reception`` ...) are reached through the name aliases in rules/object_aliases.json,
used only when the code match finds nothing.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

import numpy as np
import pandas as pd

SUMMARY_SHEET = 'Summary'
PROCESS_SHEET = 'Process CIP Water Consumption'
WATER_CLEAR_SHEET = 'Water clear'

FINAL_RINSE_SECONDS = ['Final Rinse Fill to Tank (sec)', 'Final Rinse Fill Circuit (Sec)',
                       'Final Rinse Front Mix Zone (Sec)', 'Final Rinse Circulation (Sec)']
DURATION_THRESHOLDS = {  # log activity -> threshold column
    'Pre Rinse': 'Pre Rinse Threshold (min)',
    'Caustic': 'Caustic Threshold (min)',
    'Acid': 'Acid Threshold (min)',
    'Final Rinse': 'Final Rinse Total Time (min)',
}
FLOWS = ('Supply', 'Return')
FLOW_THRESHOLDS = {flow: f'{flow} Flow Threshold (L/hr)' for flow in FLOWS}
THRESHOLD_COLUMNS = [*DURATION_THRESHOLDS.values(), *FLOW_THRESHOLDS.values()]
SUMMARY_RENAMES = {
    'New Proposed Pre Rinse total time(Min)': 'Pre Rinse Threshold (min)',
    'Caustic- Total Time': 'Caustic Threshold (min)',
    'Acid Total Time': 'Acid Threshold (min)',
    'Suppy Flow (L/Hr)': 'Supply Flow Threshold (L/hr)',
    'Return Flow (L/Hr)': 'Return Flow Threshold (L/hr)',
}
# Process-sheet step columns per phase, used to cross-check the Summary totals
PHASE_STEP_COLUMNS = {
    'Pre Rinse': ['Pre Rinse Fill to Tank (Sec)', 'Pre Rinse Fill Circuit (Sec)',
                  'Pre Rinse Front Mix Zone (Sec)', 'PreRinse Circulation(sec)'],
    'Caustic': ['Caustic Fill to Tank (Sec)', 'Caustic Fill Circuit (Sec)',
                'Caustic Front Mix Zone (Sec)', 'Caustic Circulation (Sec)'],
    'Mid Rinse': ['Mid Rinse Fill to Tank (Sec)', 'Mid Rinse Fill Circuit (Sec)',
                  'Mid Rinse Front Mix Zone (Sec)', 'Mid rinse Circulation (sec)'],
    'Acid': ['Acid Circulation'],
}
EQUIPMENT_CODE = re.compile(r'C\d{2}[A-Z]\d{2}')


def object_key(value) -> str:
    """Equipment code shared by log and workbook names, e.g. C02C01."""
    match = EQUIPMENT_CODE.search(str(value).upper())
    return match.group(0) if match else str(value).strip().upper()


def build_thresholds(summary: pd.DataFrame, process: pd.DataFrame) -> pd.DataFrame:
    thresholds = summary.merge(process[['Object', 'Suppy Flow (L/Hr)', 'Return Flow (L/Hr)', *FINAL_RINSE_SECONDS]],
                               on='Object', how='left', validate='one_to_one')
    thresholds['Final Rinse Total Time (min)'] = thresholds[FINAL_RINSE_SECONDS].sum(axis=1, min_count=4) / 60
    thresholds = thresholds.rename(columns=SUMMARY_RENAMES)
    thresholds['Object Key'] = thresholds['Object'].map(object_key)
    return thresholds


def reference_object_for_log(log_object, thresholds: pd.DataFrame, aliases: dict | None = None):
    """Workbook object for a log object, or NaN when there is no match.

    The equipment code decides; ``aliases`` (log name -> workbook name) is the fallback when no code matches.
    """
    candidates = thresholds.loc[thresholds['Object Key'].eq(object_key(log_object)), 'Object']
    if len(candidates) == 0:
        alias = (aliases or {}).get(str(log_object).strip())
        return alias if alias is not None and thresholds['Object'].eq(alias).any() else np.nan
    if len(candidates) == 1:
        return candidates.iloc[0]
    # Same code used twice (e.g. C10W02): pick the name sharing the most words with the log object
    log_text = str(log_object).upper()
    scores = candidates.map(lambda name: sum(token in log_text for token in re.findall(r'[A-Z0-9]+', name.upper())))
    return candidates[scores.idxmax()]


@dataclass
class Reference:
    """Contents of the reference workbook, ready for alerting."""
    summary: pd.DataFrame
    process: pd.DataFrame
    thresholds: pd.DataFrame
    water_clear: pd.DataFrame | None = None
    aliases: dict = field(default_factory=dict)   # log object -> workbook object, when the code cannot match
    _match_cache: dict = field(default_factory=dict, repr=False)

    @cached_property
    def lookup(self) -> pd.DataFrame:
        """Thresholds indexed by workbook object name."""
        return self.thresholds.set_index('Object')

    @cached_property
    def proposed_timer_change(self) -> pd.Timestamp:
        """Earliest date the proposed pre-rinse timers were applied (NaT when unknown)."""
        if self.water_clear is None or 'Date of Change' not in self.water_clear:
            return pd.NaT
        return pd.to_datetime(self.water_clear['Date of Change'], errors='coerce').min()

    def match(self, log_object) -> str | None:
        """Workbook object for a log object name, cached; None when unmatched."""
        if log_object not in self._match_cache:
            found = reference_object_for_log(log_object, self.thresholds, self.aliases)
            self._match_cache[log_object] = None if pd.isna(found) else found
        return self._match_cache[log_object]

    def limits_for(self, reference_object: str | None) -> dict[str, float]:
        """Duration thresholds (min) per activity and flow thresholds (L/hr) for one object.

        Missing values are NaN, which never breach (same as the notebook).
        """
        if reference_object is None or reference_object not in self.lookup.index:
            return {}
        row = self.lookup.loc[reference_object]
        limits = {phase: _float(row[col]) for phase, col in DURATION_THRESHOLDS.items()}
        limits.update({f'{flow} Flow': _float(row[col]) for flow, col in FLOW_THRESHOLDS.items()})
        return limits


def _float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.nan


def load_reference(path: Path, aliases: dict | None = None) -> Reference:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f'Reference workbook not found: {path.resolve()}')
    sheets = pd.ExcelFile(path)
    summary = pd.read_excel(sheets, sheet_name=SUMMARY_SHEET)
    process = pd.read_excel(sheets, sheet_name=PROCESS_SHEET)
    water_clear = (pd.read_excel(sheets, sheet_name=WATER_CLEAR_SHEET)
                   if WATER_CLEAR_SHEET in sheets.sheet_names else None)
    thresholds = build_thresholds(summary, process)
    aliases = dict(aliases or {})
    missing = sorted(set(aliases.values()) - set(thresholds['Object']))
    if missing:
        raise ValueError(f'Object aliases point to workbook objects that do not exist: {missing}')
    return Reference(summary=summary, process=process, thresholds=thresholds, water_clear=water_clear,
                     aliases=aliases)


def match_objects(logs: pd.DataFrame, reference: Reference) -> pd.DataFrame:
    """Add 'Object Key' and 'Reference Object' columns to the log records."""
    logs['Object Key'] = logs['Object'].map(object_key)
    mapping = {obj: reference.match(obj) for obj in logs['Object'].unique()}
    logs['Reference Object'] = logs['Object'].map(mapping)
    return logs

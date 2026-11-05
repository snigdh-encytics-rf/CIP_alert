"""Split the logs into cycles and measure each activity (notebook section 4).

A cycle is a continuous run of records for one object; a gap of more than
``cycle_gap_minutes`` starts the next one.

``Time In Seconds`` counts up through the whole cycle, so an activity's
time = last counter - first counter in that activity. Flows are the median over
the whole cycle.
"""
from __future__ import annotations

import pandas as pd

from .config import ACTIVITIES

CYCLE_KEYS = ['Line', 'Object', 'Reference Object', 'Object Key', 'Cycle Number']


def assign_cycles(logs: pd.DataFrame, gap_minutes: float) -> pd.DataFrame:
    """Add 'Cycle Number' (0, 1, 2 ... per line and object). Logs must be sorted by time."""
    gap = logs.groupby(['Line', 'Object'])['Date Time'].diff().gt(pd.Timedelta(minutes=gap_minutes))
    logs['Cycle Number'] = gap.groupby([logs['Line'], logs['Object']]).cumsum().astype(int)
    return logs


def activity_minutes(logs: pd.DataFrame, activities=ACTIVITIES) -> pd.DataFrame:
    """Minutes spent in each activity per cycle (last counter - first counter)."""
    seconds = logs.groupby(CYCLE_KEYS + ['Activity'], dropna=False)['Time In Seconds'].agg(['min', 'max'])
    minutes = ((seconds['max'] - seconds['min']) / 60).where(lambda m: m >= 0)
    return minutes.unstack('Activity').reindex(columns=list(activities))


def build_cycle_results(logs: pd.DataFrame, activities=ACTIVITIES) -> pd.DataFrame:
    """One row per cycle: timing, recipes, median flows and minutes per activity."""
    cycle_info = logs.groupby(CYCLE_KEYS, dropna=False).agg(**{
        'Cycle Start': ('Date Time', 'min'),
        'Cycle End': ('Date Time', 'max'),
        'Record Count': ('Date Time', 'size'),
        'Recipes': ('Recipe', lambda s: ', '.join(sorted(set(s.dropna().astype(str))))),
        'Supply Flow Actual (L/hr)': ('Supply Flow', 'median'),
        'Return Flow Actual (L/hr)': ('Return Flow', 'median'),
    })
    return cycle_info.join(activity_minutes(logs, activities)).reset_index()


def cycle_minutes(cycle_results: pd.DataFrame) -> pd.Series:
    return (cycle_results['Cycle End'] - cycle_results['Cycle Start']).dt.total_seconds() / 60


def find_merged_runs(logs: pd.DataFrame) -> pd.DataFrame:
    """Cycles whose elapsed counter restarts, i.e. two CIP runs share one cycle."""
    counter_drop = logs.groupby(CYCLE_KEYS, dropna=False)['Time In Seconds'].diff().lt(0)
    return logs.loc[counter_drop, CYCLE_KEYS].drop_duplicates()


def cycle_records(logs: pd.DataFrame, log_object: str, cycle_number: int) -> pd.DataFrame:
    """Raw records of one cycle, in time order."""
    selected = logs['Object'].eq(log_object) & logs['Cycle Number'].eq(cycle_number)
    return logs[selected].sort_values('Date Time').copy()

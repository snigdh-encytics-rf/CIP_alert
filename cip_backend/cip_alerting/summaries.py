"""Summaries (notebook section 7): cycles per object by status, cycles and alerts per day."""
from __future__ import annotations

import pandas as pd

from .rules import STATUS_ALERT


def cycle_counts(cycle_results: pd.DataFrame) -> pd.DataFrame:
    counts = pd.crosstab([cycle_results['Line'], cycle_results['Object']], cycle_results['Status'])
    counts.insert(0, 'Cycles', counts.sum(axis=1))
    return counts


def alerts_per_day(cycle_results: pd.DataFrame) -> pd.DataFrame:
    return (cycle_results.assign(Date=cycle_results['Cycle Start'].dt.date)
            .groupby('Date')
            .agg(**{'Cycles': ('Status', 'size'),
                    'Cycles with Alerts': ('Status', lambda s: s.eq(STATUS_ALERT).sum()),
                    'Alert Count': ('Alert Count', 'sum')})
            .reset_index())

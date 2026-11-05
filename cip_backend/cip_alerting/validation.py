"""Validation log used by every pipeline stage.

An ERROR stops the run (raises ``ValidationError``); a WARNING is recorded and
reported at the end. Re-running a stage replaces that stage's earlier results.
"""
from __future__ import annotations

import logging
from typing import Iterable

import pandas as pd

logger = logging.getLogger(__name__)


class ValidationError(AssertionError):
    """A check at ERROR level failed."""


def preview(values: Iterable, limit: int = 5) -> str:
    values = [str(value) for value in values]
    more = f' (+{len(values) - limit} more)' if len(values) > limit else ''
    return ', '.join(values[:limit]) + more


class ValidationLog:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def start_stage(self, stage: str) -> None:
        self.rows[:] = [row for row in self.rows if row['Stage'] != stage]

    def check(self, stage: str, name: str, passed, detail: str = '', level: str = 'ERROR') -> bool:
        passed = bool(passed)
        self.rows.append({'Stage': stage, 'Check': name,
                          'Status': 'PASS' if passed else level,
                          'Detail': '' if passed else detail})
        if not passed:
            message = f'[{level}] {name}' + (f': {detail}' if detail else '')
            if level == 'ERROR':
                logger.error(message)
                raise ValidationError(f'{stage} check failed: {name}. {detail}')
            logger.warning(message)
        return passed

    def stage_counts(self, stage: str) -> dict[str, int]:
        counts = pd.Series([row['Status'] for row in self.rows if row['Stage'] == stage]).value_counts()
        return {status: int(count) for status, count in counts.items()}

    def report(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows, columns=['Stage', 'Check', 'Status', 'Detail'])

    def summary_table(self) -> pd.DataFrame:
        """Checks per stage and status (notebook section 8)."""
        return self.report().groupby(['Stage', 'Status'], sort=False).size().unstack(fill_value=0)

    def issues(self) -> pd.DataFrame:
        """Every check that did not pass."""
        report = self.report()
        return report[report['Status'].ne('PASS')].reset_index(drop=True)

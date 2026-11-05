"""Data shapes for the live system: one reading per object per minute, alerts and events."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping

import pandas as pd

# Log column name -> Reading attribute
TEXT_FIELDS = {'Line': 'line', 'Object': 'object', 'Recipe': 'recipe', 'Activity': 'activity', 'Step': 'step'}
NUMERIC_FIELDS = {
    'Time In Seconds': 'time_in_seconds',
    'Temp SP': 'temp_sp',
    'Return Temp': 'return_temp',
    'Supply Flow': 'supply_flow',
    'Return Flow': 'return_flow',
    'Min Return Flow SP': 'min_return_flow_sp',
    'Return Conducivity': 'return_conductivity',
    'Caustic SP': 'caustic_sp',
    'Acid SP': 'acid_sp',
    'Final Rinse Cond. SP': 'final_rinse_cond_sp',
    'Flip': 'flip',
}
NAN = math.nan


def _text(value) -> str | None:
    return None if value is None or (not isinstance(value, str) and pd.isna(value)) else str(value)


def _number(value) -> float:
    if value is None:
        return NAN
    try:
        return float(value)
    except (TypeError, ValueError):
        return NAN


def is_missing(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def json_safe(value):
    """NaN -> None and timestamps -> ISO strings, recursively."""
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


@dataclass(frozen=True)
class Reading:
    """One ping from one CIP object: the same fields as one row of the station log."""
    timestamp: pd.Timestamp
    line: str | None
    object: str | None
    recipe: str | None = None
    activity: str | None = None
    step: str | None = None
    time_in_seconds: float = NAN   # elapsed counter; counts up through the whole CIP run
    temp_sp: float = NAN
    return_temp: float = NAN
    supply_flow: float = NAN
    return_flow: float = NAN
    min_return_flow_sp: float = NAN
    return_conductivity: float = NAN
    caustic_sp: float = NAN
    acid_sp: float = NAN
    final_rinse_cond_sp: float = NAN
    flip: float = NAN
    source_file: str | None = None
    excel_row: int | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> 'Reading':
        """Build from a dict keyed by log column names (e.g. a logs DataFrame row)."""
        values = {attr: _text(row.get(col)) for col, attr in TEXT_FIELDS.items()}
        values.update({attr: _number(row.get(col)) for col, attr in NUMERIC_FIELDS.items()})
        excel_row = row.get('Excel Row')
        return cls(timestamp=pd.Timestamp(row['Date Time']), **values,
                   source_file=_text(row.get('Source File')),
                   excel_row=None if is_missing(excel_row) else int(excel_row))

    def value(self, column: str):
        """Value by log column name, e.g. reading.value('Return Temp')."""
        if column == 'Date Time':
            return self.timestamp
        attr = NUMERIC_FIELDS.get(column) or TEXT_FIELDS.get(column)
        if attr is None:
            raise KeyError(column)
        return getattr(self, attr)

    def signals(self) -> dict[str, float]:
        """All numeric values keyed by log column name."""
        return {col: getattr(self, attr) for col, attr in NUMERIC_FIELDS.items()}

    def to_dict(self) -> dict:
        return json_safe({'Date Time': self.timestamp,
                          **{col: getattr(self, attr) for col, attr in TEXT_FIELDS.items()},
                          **self.signals(), 'Source File': self.source_file, 'Excel Row': self.excel_row})


@dataclass
class Alert:
    """One duration breach in one cycle (or, with CIP_PROPOSED_RULES=alert, one proposed T/A/C rule met).
    Raised the minute the limit is crossed."""
    id: str
    line: str
    object: str
    reference_object: str
    cycle_number: int
    phase: str                      # log activity, e.g. 'Caustic'
    alert_type: str                 # e.g. 'Caustic duration'
    raised_at: pd.Timestamp         # timestamp of the first reading over the limit
    step_at_raise: str | None
    actual_minutes: float           # keeps growing until the activity or cycle ends
    threshold_minutes: float
    limit_minutes: float
    tolerance: float                # duration tolerance used for the limit
    rule: str
    tact_at_raise: list[dict]       # see rules.tact_panel
    severity: str | None = None     # not defined by the notebook; open question for Danone
    acknowledged_at: pd.Timestamp | None = None
    acknowledged_by: str | None = None
    acknowledged_at_wall: str | None = None   # wall clock (UTC ISO) of the acknowledgement, when stored
    acknowledgement_note: str | None = None
    cycle_closed_at: pd.Timestamp | None = None
    unit: str = 'minutes'
    # Proposed TACT rules (proposed_rules.py; CIP_PROPOSED_RULES=alert only). For these alerts actual_minutes is
    # the time the reading has stayed below the proposed limit and threshold/limit_minutes the rule's minutes.
    parameter: str = 'Time'         # TACT parameter that raised the alert
    rule_id: str | None = None
    proposed: bool = False          # True: rule pending Danone sign-off, never presented as approved
    measured: dict | None = None    # proposed: signal, value, set point, limit, unit, streak start, rule text

    @property
    def title(self) -> str:
        if self.proposed and self.measured:
            return f'{self.measured["title"]} (proposed rule)'
        return f'{self.phase} ran over its time limit'

    @property
    def explanation(self) -> str:
        if self.proposed and self.measured:
            from .proposed_rules import explain_proposed_alert
            return explain_proposed_alert(self)
        from .rules import explain_duration_alert
        return explain_duration_alert(self.phase, self.actual_minutes, self.threshold_minutes, self.tolerance)

    @property
    def is_acknowledged(self) -> bool:
        return self.acknowledged_at is not None

    def to_dict(self) -> dict:
        return json_safe({
            'id': self.id, 'line': self.line, 'objectId': self.object, 'referenceObject': self.reference_object,
            'cycleNumber': self.cycle_number, 'phase': self.phase, 'alertType': self.alert_type,
            'title': self.title, 'explanation': self.explanation, 'rule': self.rule,
            'severity': self.severity, 'raisedAt': self.raised_at, 'stepAtRaise': self.step_at_raise,
            'actualMinutes': self.actual_minutes, 'thresholdMinutes': self.threshold_minutes,
            'limitMinutes': self.limit_minutes, 'unit': self.unit, 'tact': self.tact_at_raise,
            'acknowledgedAt': self.acknowledged_at, 'acknowledgedBy': self.acknowledged_by,
            'acknowledgedAtWall': self.acknowledged_at_wall, 'acknowledgementNote': self.acknowledgement_note,
            'cycleClosedAt': self.cycle_closed_at,
            'parameter': self.parameter, 'proposed': self.proposed, 'ruleId': self.rule_id,
            'ruleStatus': 'proposed' if self.proposed else None,
            'signOff': 'Proposed — pending Danone sign-off' if self.proposed else None,
            'measured': self.measured,
        })


# Event kinds emitted by the live monitor
CYCLE_STARTED = 'cycle_started'
ACTIVITY_STARTED = 'activity_started'
ALERT_RAISED = 'alert_raised'
CYCLE_CLOSED = 'cycle_closed'
DATA_QUALITY = 'data_quality'


@dataclass
class Event:
    kind: str
    at: pd.Timestamp
    line: str | None
    object: str | None
    cycle_number: int | None = None
    alert: Alert | None = None
    detail: dict = field(default_factory=dict)
    summary: dict | None = field(default=None, repr=False, compare=False)  # cycle_closed: the full cycle row

    def to_dict(self) -> dict:
        return json_safe({'kind': self.kind, 'at': self.at, 'line': self.line, 'object': self.object,
                          'cycleNumber': self.cycle_number,
                          'alert': self.alert.to_dict() if self.alert else None, 'detail': self.detail})

"""Proposed TACT rules for Temperature, Action (flow) and Concentration (conductivity).

The notebook only alerts on Time (activity duration). These rules are a PROPOSAL derived from the July 2026
logs (see ``docs/TACT_rules_proposal.md``); Danone's process engineers have not approved them. Every output
of this module carries ``status: 'proposed'`` and the label ``SIGN_OFF``.

Rule definitions live in ``rules/proposed_tact_rules.json``. Each rule watches one signal of one activity
during one step (circulation) and is met when the reading stays below a limit derived from the PLC set point
for ``minutes`` or longer::

    below_set_point_minus        value < set point - belowBy            (temperature, in °C)
    below_percent_of_set_point   value < set point x percent / 100      (flow, conductivity)

- The first ``graceMinutes`` of each continuous circulation block are not checked (ramp-up allowance).
- A streak is consecutive readings below the limit inside one block; its length is the time from its first
  to its latest reading. A reading that is missing, outside the plausible sensor range (``VALID_RANGES``), at
  or above the limit, or in another step ends the streak.
- One alert per rule per cycle, at the first reading where the streak reaches ``minutes``.
- Only objects with reference thresholds are checked (the same objects the Time rule monitors).

``CIP_PROPOSED_RULES`` (``Settings.proposed_rules``) selects the mode:

- ``off``    rules are not loaded.
- ``show``   (default) the debug panel shows the proposed evaluation next to each reading; no alerts.
- ``alert``  the rules also raise alerts (tagged ``proposed``). Changes alert counts, so never the default.

The live monitor (``ProposedTracker``, one reading at a time) and the batch pipeline (``evaluate_batch``,
pandas) implement the same definition; ``tests/test_proposed_rules.py`` checks they raise the same alerts.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from .config import VALID_RANGES, default_proposed_rules

MODES = ('off', 'show', 'alert')
DEFAULT_MODE = 'show'
STATUS_PROPOSED = 'proposed'
SIGN_OFF = 'Proposed — pending Danone sign-off'
DEFAULT_RULES_FILE = Path(__file__).resolve().parents[1] / 'rules' / 'proposed_tact_rules.json'

BELOW_SET_POINT_MINUS = 'below_set_point_minus'
BELOW_PERCENT_OF_SET_POINT = 'below_percent_of_set_point'
CONDITIONS = (BELOW_SET_POINT_MINUS, BELOW_PERCENT_OF_SET_POINT)
# TACT parameter -> log signal (same mapping as rules.TACT_SIGNALS)
PARAMETER_SIGNALS = {'Temperature': 'Return Temp', 'Action': 'Return Flow', 'Concentration': 'Return Conducivity'}
SIGNAL_LABELS = {'Return Temp': 'Return temperature', 'Return Flow': 'Return flow',
                 'Return Conducivity': 'Return conductivity'}
ACTIVITY_TEXT = {'Pre Rinse': 'pre-rinse', 'Caustic': 'caustic wash', 'Mid Rinse': 'mid rinse', 'Acid': 'acid wash',
                 'Final Rinse': 'final rinse'}
SET_POINTS = {'Temp SP', 'Min Return Flow SP', 'Caustic SP', 'Acid SP', 'Final Rinse Cond. SP'}

# Tile evaluation at one reading
VIEW_OUTSIDE = 'outside'        # reading below the proposed limit
VIEW_WITHIN = 'within'          # reading at or above the proposed limit
VIEW_NOT_CHECKED = 'not_checked'  # other step, ramp-up allowance, missing or invalid reading


def default_mode() -> str:
    """$CIP_PROPOSED_RULES (off, show, alert); 'show' when unset."""
    return default_proposed_rules()


def default_rules_file() -> Path:
    env = os.environ.get('CIP_PROPOSED_RULES_FILE', '').strip()
    return Path(env) if env else DEFAULT_RULES_FILE


def _missing(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def is_valid(signal: str, value) -> bool:
    """A real reading: present and inside the plausible sensor range (same limits as the data checks)."""
    if _missing(value):
        return False
    low, high = VALID_RANGES.get(signal, (-math.inf, math.inf))
    return low <= value <= high


@dataclass(frozen=True)
class ProposedRule:
    id: str
    parameter: str          # Temperature, Action or Concentration
    activity: str           # log activity, e.g. 'Caustic'
    step: str               # log step, e.g. 'Circulation'
    signal: str             # log column, e.g. 'Return Temp'
    set_point: str          # log column, e.g. 'Temp SP'
    condition: str
    amount: float           # belowBy (unit) or percent of set point
    minutes: float          # streak length that meets the rule
    grace_minutes: float    # start of each circulation block that is not checked
    unit: str
    title: str
    rationale: str = ''
    status: str = STATUS_PROPOSED
    projected_alerts: int | None = None
    extra: dict = field(default_factory=dict, compare=False, repr=False)

    def limit(self, set_point):
        """Proposed limit for a set point (scalar or pandas Series); NaN set point -> NaN."""
        if self.condition == BELOW_SET_POINT_MINUS:
            return set_point - self.amount
        return set_point * (self.amount / 100.0)

    def is_low(self, value, set_point) -> bool:
        """True when a valid reading is below the proposed limit. Missing or invalid values never count."""
        if not is_valid(self.signal, value) or _missing(set_point):
            return False
        return bool(value < self.limit(set_point))

    @property
    def limit_text(self) -> str:
        if self.condition == BELOW_SET_POINT_MINUS:
            return f'set point − {self.amount:g} {self.unit}'
        return f'{self.amount:g}% of set point'

    @property
    def text(self) -> str:
        """One line for people, e.g. 'Return temperature more than 5 °C below its set point for 5 min or longer
        during caustic wash circulation (first 5 min not checked)'."""
        signal = SIGNAL_LABELS.get(self.signal, self.signal)
        if self.condition == BELOW_SET_POINT_MINUS:
            how = f'more than {self.amount:g} {self.unit} below its set point'
        else:
            how = f'below {self.amount:g}% of its set point'
        grace = f' (first {self.grace_minutes:g} min not checked)' if self.grace_minutes else ''
        return (f'{signal} {how} for {self.minutes:g} min or longer during '
                f'{ACTIVITY_TEXT.get(self.activity, self.activity)} {self.step.lower()}{grace}')

    def to_dict(self) -> dict:
        return {'id': self.id, 'parameter': self.parameter, 'activity': self.activity, 'step': self.step,
                'signal': self.signal, 'setPoint': self.set_point, 'condition': self.condition,
                'thresholds': self._thresholds(), 'unit': self.unit, 'title': self.title, 'rule': self.text,
                'limitText': self.limit_text, 'rationale': self.rationale, 'status': self.status,
                'projectedAlerts': self.projected_alerts, 'signOff': SIGN_OFF}

    def _thresholds(self) -> dict:
        key = 'belowBy' if self.condition == BELOW_SET_POINT_MINUS else 'percent'
        return {key: self.amount, 'minutes': self.minutes, 'graceMinutes': self.grace_minutes}


class RuleFileError(ValueError):
    """The proposed-rules file is missing a field or has a value the monitor cannot use."""


def _parse_rule(raw: dict, index: int) -> ProposedRule:
    where = f'rule {index + 1} ({raw.get("id", "no id")})'

    def need(key, kind=str):
        if key not in raw:
            raise RuleFileError(f'{where}: "{key}" is required')
        value = raw[key]
        if not isinstance(value, kind) or isinstance(value, bool):
            raise RuleFileError(f'{where}: "{key}" must be {kind.__name__ if isinstance(kind, type) else "a number"}')
        return value

    rule_id = need('id')
    parameter = need('parameter')
    if parameter not in PARAMETER_SIGNALS:
        raise RuleFileError(f'{where}: parameter must be one of {", ".join(PARAMETER_SIGNALS)}')
    signal = raw.get('signal', PARAMETER_SIGNALS[parameter])
    if signal != PARAMETER_SIGNALS[parameter]:
        raise RuleFileError(f'{where}: {parameter} is read from {PARAMETER_SIGNALS[parameter]!r}, not {signal!r}')
    set_point = need('setPoint')
    if set_point not in SET_POINTS:
        raise RuleFileError(f'{where}: setPoint must be one of {", ".join(sorted(SET_POINTS))}')
    condition = need('condition')
    if condition not in CONDITIONS:
        raise RuleFileError(f'{where}: condition must be one of {", ".join(CONDITIONS)}')
    status = need('status')
    if status != STATUS_PROPOSED:
        # Approval is a deliberate code and documentation change, not a flag in this file.
        raise RuleFileError(f'{where}: status must be "proposed"; approved rules do not belong in this file')
    thresholds = need('thresholds', dict)
    amount_key = 'belowBy' if condition == BELOW_SET_POINT_MINUS else 'percent'
    numbers = {}
    for key in (amount_key, 'minutes', 'graceMinutes'):
        value = thresholds.get(key, 0 if key == 'graceMinutes' else None)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
            raise RuleFileError(f'{where}: thresholds.{key} must be a number of at least 0')
        numbers[key] = float(value)
    if condition == BELOW_PERCENT_OF_SET_POINT and not 0 < numbers['percent'] <= 100:
        raise RuleFileError(f'{where}: thresholds.percent must be above 0 and at most 100')
    projected = raw.get('projectedAlerts')
    return ProposedRule(
        id=rule_id, parameter=parameter, activity=need('activity'), step=raw.get('step', 'Circulation'),
        signal=signal, set_point=set_point, condition=condition, amount=numbers[amount_key],
        minutes=numbers['minutes'], grace_minutes=numbers['graceMinutes'], unit=need('unit'),
        title=need('title'), rationale=str(raw.get('rationale', '')), status=status,
        projected_alerts=projected if isinstance(projected, int) else None,
        extra={k: v for k, v in raw.items() if k in ('evidence', 'openQuestions')})


@dataclass
class RuleSet:
    mode: str
    rules: list[ProposedRule]
    source: str | None = None
    meta: dict = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.mode != 'off' and bool(self.rules)

    @property
    def alerting(self) -> bool:
        return self.mode == 'alert' and bool(self.rules)

    def for_activity(self, activity: str | None) -> list[ProposedRule]:
        return [r for r in self.rules if r.activity == activity] if self.active else []

    def rule_for(self, activity: str | None, parameter: str) -> ProposedRule | None:
        return next((r for r in self.for_activity(activity) if r.parameter == parameter), None)

    def get(self, rule_id: str) -> ProposedRule | None:
        return next((r for r in self.rules if r.id == rule_id), None)

    def status(self) -> dict:
        return {'mode': self.mode, 'rules': len(self.rules) if self.mode != 'off' else 0,
                'raisesAlerts': self.alerting, 'status': STATUS_PROPOSED, 'signOff': SIGN_OFF,
                'basis': self.meta.get('basis')}

    def to_dict(self) -> dict:
        return {**self.status(), 'definitions': [r.to_dict() for r in self.rules] if self.mode != 'off' else []}


def load_rule_set(mode: str | None = None, path: str | os.PathLike | None = None) -> RuleSet:
    """Rules from ``path`` (default ``rules/proposed_tact_rules.json``) in ``mode`` (default $CIP_PROPOSED_RULES)."""
    mode = (mode or default_mode()).strip().lower()
    if mode not in MODES:
        raise ValueError(f'CIP_PROPOSED_RULES must be one of {", ".join(MODES)}, not {mode!r}')
    if mode == 'off':
        return RuleSet(mode='off', rules=[])
    path = Path(path) if path else default_rules_file()
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise RuleFileError(f'Proposed rules file not found: {path}') from None
    except json.JSONDecodeError as exc:
        raise RuleFileError(f'{path}: not valid JSON ({exc})') from None
    if not isinstance(data, dict) or not isinstance(data.get('rules'), list):
        raise RuleFileError(f'{path}: expected an object with a "rules" list')
    rules = [_parse_rule(raw, i) for i, raw in enumerate(data['rules'])]
    ids = [r.id for r in rules]
    if len(set(ids)) != len(ids):
        raise RuleFileError(f'{path}: rule ids must be unique')
    keys = [(r.activity, r.parameter) for r in rules]
    if len(set(keys)) != len(keys):
        raise RuleFileError(f'{path}: at most one rule per activity and parameter')
    meta = {k: v for k, v in data.items() if k != 'rules'}
    return RuleSet(mode=mode, rules=rules, source=str(path), meta=meta)


# ---------------------------------------------------------------------------
# Live: one tracker per running cycle
# ---------------------------------------------------------------------------
@dataclass
class Fired:
    rule: ProposedRule
    streak_start: pd.Timestamp
    minutes: float


class ProposedTracker:
    """Streak state of the proposed rules for one cycle, updated one reading at a time."""

    def __init__(self, rule_set: RuleSet):
        self.rule_set = rule_set
        self.block_key: tuple | None = None
        self.block_start: pd.Timestamp | None = None
        self.started = False
        self.streak_start: dict[str, pd.Timestamp] = {}   # rule id -> first reading of the running streak
        self.met: dict[str, pd.Timestamp] = {}            # rule id -> reading where the rule was met
        self.open_streak: dict[str, pd.Timestamp] = {}    # rule id -> start of the streak that met the rule, while it lasts

    def _elapsed(self, at: pd.Timestamp) -> float:
        return (at - self.block_start).total_seconds() / 60

    def update(self, reading) -> list[Fired]:
        """Advance with the cycle's next reading. Returns the rules met at this reading (each once per cycle)."""
        key = (reading.activity, reading.step)
        if not self.started or key != self.block_key:
            self.started, self.block_key, self.block_start = True, key, reading.timestamp
            self.streak_start.clear()
            self.open_streak.clear()
        fired = []
        for rule in self.rule_set.rules if self.rule_set.active else ():
            counted = (reading.activity == rule.activity and reading.step == rule.step
                       and self._elapsed(reading.timestamp) >= rule.grace_minutes
                       and rule.is_low(reading.value(rule.signal), reading.value(rule.set_point)))
            if not counted:
                self.streak_start.pop(rule.id, None)
                self.open_streak.pop(rule.id, None)
                continue
            start = self.streak_start.setdefault(rule.id, reading.timestamp)
            minutes = (reading.timestamp - start).total_seconds() / 60
            if rule.id not in self.met and minutes >= rule.minutes:
                self.met[rule.id] = reading.timestamp
                self.open_streak[rule.id] = start
                fired.append(Fired(rule, start, minutes))
        return fired

    def streak_minutes(self, rule: ProposedRule, at: pd.Timestamp) -> float | None:
        start = self.streak_start.get(rule.id)
        return None if start is None else (at - start).total_seconds() / 60

    def view(self, reading) -> dict[str, dict]:
        """Proposed evaluation of each TACT parameter at this reading (for the debug panel tiles)."""
        out = {}
        for rule in self.rule_set.for_activity(reading.activity):
            out[rule.parameter] = evaluate_reading(rule, reading.value(rule.signal), reading.value(rule.set_point),
                                                   reading.step, self._elapsed(reading.timestamp)
                                                   if self.block_start is not None else None,
                                                   self.streak_minutes(rule, reading.timestamp),
                                                   self.met.get(rule.id))
        return out


def evaluate_reading(rule: ProposedRule, value, set_point, step: str | None, block_minutes: float | None,
                     streak_minutes: float | None = None, met_at: pd.Timestamp | None = None) -> dict:
    """Tile data: is one reading inside the proposed limit? ``status`` is outside, within or not_checked."""
    limit = None if _missing(set_point) else float(rule.limit(set_point))
    reason = None
    if step != rule.step:
        reason = f'Checked during {rule.step.lower()} only'
    elif block_minutes is not None and block_minutes < rule.grace_minutes:
        reason = f'First {rule.grace_minutes:g} min of {rule.step.lower()} are not checked (ramp-up allowance)'
    elif _missing(value):
        reason = 'No reading'
    elif not is_valid(rule.signal, value):
        reason = 'Invalid reading (outside the plausible sensor range)'
    elif limit is None:
        reason = 'No set point'
    status = VIEW_NOT_CHECKED if reason else (VIEW_OUTSIDE if value < limit else VIEW_WITHIN)
    return {'ruleId': rule.id, 'status': status, 'limit': limit, 'limitText': rule.limit_text,
            'rule': rule.text, 'minutes': rule.minutes, 'graceMinutes': rule.grace_minutes, 'reason': reason,
            'streakMinutes': streak_minutes, 'metAt': met_at, 'ruleStatus': STATUS_PROPOSED, 'signOff': SIGN_OFF}


# ---------------------------------------------------------------------------
# Batch: the same rules over whole log tables (pandas)
# ---------------------------------------------------------------------------
PROPOSED_ALERT_COLUMNS = ['Line', 'Object', 'Reference Object', 'Cycle Number', 'Rule', 'Parameter', 'Activity',
                          'Raised At', 'Step', 'Value', 'Set Point', 'Limit', 'Unit', 'Streak Start',
                          'Minutes At Raise', 'Minutes Below']
_NONE = '\x00none'


def evaluate_batch(logs: pd.DataFrame, rule_set: RuleSet) -> pd.DataFrame:
    """One row per proposed rule met per cycle. ``logs`` need 'Cycle Number' and 'Reference Object' columns
    (``cycles.assign_cycles`` and ``thresholds.match_objects``)."""
    if not rule_set.active or logs.empty:
        return pd.DataFrame(columns=PROPOSED_ALERT_COLUMNS)
    df = logs.sort_values(['Line', 'Object', 'Date Time'], kind='stable')
    t = df['Date Time']
    cycle = df['Line'].astype(str) + '\x01' + df['Object'].astype(str) + '\x01' + df['Cycle Number'].astype(str)
    activity = df['Activity'].astype(object).where(df['Activity'].notna(), _NONE)
    step = df['Step'].astype(object).where(df['Step'].notna(), _NONE)
    new_block = (cycle != cycle.shift()) | (activity != activity.shift()) | (step != step.shift())
    block = new_block.cumsum()
    block_minutes = (t - t.groupby(block).transform('first')).dt.total_seconds() / 60
    monitored = df['Reference Object'].notna()
    frames = []
    for rule in rule_set.rules:
        value, set_point = df[rule.signal], df[rule.set_point]
        low, high = VALID_RANGES.get(rule.signal, (-math.inf, math.inf))
        counted = (monitored & activity.eq(rule.activity) & step.eq(rule.step)
                   & (block_minutes >= rule.grace_minutes)
                   & value.notna() & value.between(low, high) & set_point.notna()
                   & (value < rule.limit(set_point)))
        run = (new_block | counted.ne(counted.shift())).cumsum()
        run_start = t.groupby(run).transform('first')
        run_end = t.groupby(run).transform('last')
        span = (t - run_start).dt.total_seconds() / 60
        hit = counted & (span >= rule.minutes)
        if not hit.any():   # (assigning Series to an empty frame would adopt their index)
            continue
        fired = df[hit].assign(_cycle=cycle[hit], _span=span[hit], _start=run_start[hit], _end=run_end[hit])
        first = fired.groupby('_cycle', sort=False).head(1)
        frames.append(pd.DataFrame({
            'Line': first['Line'], 'Object': first['Object'], 'Reference Object': first['Reference Object'],
            'Cycle Number': first['Cycle Number'], 'Rule': rule.id, 'Parameter': rule.parameter,
            'Activity': rule.activity, 'Raised At': first['Date Time'], 'Step': first['Step'],
            'Value': first[rule.signal], 'Set Point': first[rule.set_point],
            'Limit': rule.limit(first[rule.set_point]), 'Unit': rule.unit, 'Streak Start': first['_start'],
            'Minutes At Raise': first['_span'],
            'Minutes Below': (first['_end'] - first['_start']).dt.total_seconds() / 60,
        }))
    if not frames:
        return pd.DataFrame(columns=PROPOSED_ALERT_COLUMNS)
    out = pd.concat(frames, ignore_index=True)
    return out.sort_values(['Raised At', 'Line', 'Object', 'Rule'], kind='stable').reset_index(drop=True)[
        PROPOSED_ALERT_COLUMNS]


def projection(logs: pd.DataFrame, rule_set: RuleSet) -> dict[str, Any]:
    """Alerts each rule would raise over ``logs``: total, cycles checked, and per line."""
    alerts = evaluate_batch(logs, rule_set)
    out = {}
    for rule in rule_set.rules:
        rows = alerts[alerts['Rule'].eq(rule.id)]
        out[rule.id] = {'alerts': len(rows), 'byLine': rows['Line'].value_counts().sort_index().to_dict(),
                        'objects': int(rows['Object'].nunique())}
    return out


# ---------------------------------------------------------------------------
# Alerts raised by proposed rules (CIP_PROPOSED_RULES=alert)
# ---------------------------------------------------------------------------
def _fmt(value: float, unit: str) -> str:
    decimals = 0 if unit in ('L/hr', 'L/h') else 1
    return f'{value:,.{decimals}f} {unit}'


def measured_for(rule: ProposedRule, reading, fired: Fired) -> dict:
    """What the alert holds about the reading that met the rule."""
    set_point = reading.value(rule.set_point)
    return {'ruleId': rule.id, 'title': rule.title, 'rule': rule.text, 'parameter': rule.parameter,
            'signal': rule.signal, 'signalLabel': SIGNAL_LABELS.get(rule.signal, rule.signal),
            'setPointColumn': rule.set_point, 'value': reading.value(rule.signal), 'setPoint': set_point,
            'limit': float(rule.limit(set_point)), 'limitText': rule.limit_text, 'unit': rule.unit,
            'condition': rule.condition, 'amount': rule.amount,
            'minutes': rule.minutes, 'graceMinutes': rule.grace_minutes, 'streakStart': fired.streak_start,
            'minutesAtRaise': fired.minutes, 'step': rule.step, 'status': STATUS_PROPOSED, 'signOff': SIGN_OFF}


def explain_proposed_alert(alert) -> str:
    m = alert.measured
    unit = m['unit']
    limit = _fmt(m['limit'], unit)
    activity = ACTIVITY_TEXT.get(alert.phase, alert.phase)
    start = pd.Timestamp(m['streakStart']).strftime('%H:%M') if m.get('streakStart') is not None else None
    return (f'{m["signalLabel"]} stayed below the proposed limit of {limit} ({m["limitText"]}, set point '
            f'{_fmt(m["setPoint"], unit)}) for {alert.actual_minutes:.0f} min during {activity} '
            f'{m["step"].lower()}{f", from {start}" if start else ""}. Proposed rule {m["ruleId"]}, '
            f'derived from the July 2026 data: pending Danone sign-off.')

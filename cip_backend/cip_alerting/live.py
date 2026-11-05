"""Live monitor: the notebook's rules applied one reading at a time.

Each CIP object (IoT device) sends one ``Reading`` about every 60 seconds.
``LiveMonitor.ingest`` updates that object's running cycle and returns events:

- ``cycle_started``: first reading, or the first after a gap > cycle_gap_minutes
- ``activity_started``: the activity changed (Pre Rinse -> Caustic ...)
- ``alert_raised``: an activity's duration just crossed threshold x (1 + tolerance); with
  ``Settings.proposed_rules == 'alert'`` also a proposed T/A/C rule that was just met (``proposed_rules.py``)
- ``cycle_closed``: the cycle ended (gap seen on the next reading, or ``close_idle``)
- ``data_quality``: a validation check failed for this cycle (reported once per issue per cycle)

The rules are the same functions the batch pipeline uses, so replaying the logs
through this monitor raises exactly the batch alerts (see tests/test_parity.py).
Difference from batch: an alert is raised the minute the limit is crossed, and
its ``actual_minutes`` keeps growing until the activity ends.
"""
from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field

import pandas as pd

from .config import ACTIVITIES, EXPECTED_RECIPES, EXPECTED_STEPS, VALID_RANGES, Settings
from .models import (ACTIVITY_STARTED, ALERT_RAISED, CYCLE_CLOSED, CYCLE_STARTED, DATA_QUALITY, NAN, Alert,
                     Event, Reading, is_missing)
from .proposed_rules import Fired, ProposedTracker, RuleSet, load_rule_set, measured_for
from .rules import (FLOW_THRESHOLDS, alert_type, cycle_status, duration_limit, is_duration_breach,
                    is_flow_below_reference, rule_text, tact_panel)
from .thresholds import DURATION_THRESHOLDS, FLOWS, Reference, object_key


@dataclass
class CycleState:
    """Everything the monitor knows about one running (or closed) cycle."""
    line: str
    object: str
    object_key: str
    reference_object: str | None
    cycle_number: int
    limits: dict[str, float]
    start: pd.Timestamp
    last_seen: pd.Timestamp
    readings: list[Reading] = field(default_factory=list)
    counter_range: dict[str, list[float]] = field(default_factory=dict)  # activity -> [min, max] counter
    activity_order: list[str] = field(default_factory=list)
    current_activity: str | None = None
    last_counter: float | None = None
    recipes: set[str] = field(default_factory=set)
    alerts: dict[str, Alert] = field(default_factory=dict)  # phase -> alert
    quality: Counter = field(default_factory=Counter)
    end: pd.Timestamp | None = None
    proposed: ProposedTracker | None = None   # proposed T/A/C rules (monitored objects only)

    @property
    def has_reference(self) -> bool:
        return self.reference_object is not None

    def duration_minutes(self, activity: str) -> float:
        """Last counter - first counter for one activity, in minutes (NaN if not run)."""
        span = self.counter_range.get(activity)
        return NAN if span is None else (span[1] - span[0]) / 60

    def activity_readings(self, activity: str) -> list[Reading]:
        return [r for r in self.readings if r.activity == activity]

    def summary(self, tolerance: float) -> dict:
        """One row in the same shape as the batch ``cycle_results``."""
        row = {'Line': self.line, 'Object': self.object, 'Reference Object': self.reference_object,
               'Object Key': self.object_key, 'Cycle Number': self.cycle_number,
               'Cycle Start': self.start, 'Cycle End': self.end or self.last_seen,
               'Record Count': len(self.readings), 'Recipes': ', '.join(sorted(self.recipes))}
        for flow, attr in (('Supply', 'supply_flow'), ('Return', 'return_flow')):
            values = [getattr(r, attr) for r in self.readings if not is_missing(getattr(r, attr))]
            row[f'{flow} Flow Actual (L/hr)'] = statistics.median(values) if values else NAN
        for activity in ACTIVITIES:
            row[activity] = self.duration_minutes(activity)
        alert_count = 0
        for phase, threshold_col in DURATION_THRESHOLDS.items():
            threshold = self.limits.get(phase, NAN)
            breach = bool(is_duration_breach(row[phase], threshold, tolerance))
            row[f'{phase} Threshold (min)'] = threshold
            row[f'{phase} Breach'] = breach
            alert_count += breach
        for flow in FLOWS:
            row[FLOW_THRESHOLDS[flow]] = self.limits.get(f'{flow} Flow', NAN)
        row['Alert Count'] = alert_count   # Time rule only, as in the notebook
        row['Status'] = cycle_status(self.has_reference, alert_count)
        row['Proposed Alert Count'] = sum(a.proposed for a in self.alerts.values())
        row['Data Quality'] = dict(self.quality)
        return row


@dataclass
class ObjectState:
    line: str
    object: str
    cycles_started: int = 0
    current: CycleState | None = None
    last_closed: CycleState | None = None
    last_reading: Reading | None = None


class LiveMonitor:
    def __init__(self, reference: Reference, settings: Settings | None = None, keep_closed_cycles: int | None = None,
                 rule_set: RuleSet | None = None):
        self.reference = reference
        self.settings = settings or Settings()
        # Proposed T/A/C rules: 'show' feeds the debug panel only; 'alert' also raises alerts.
        self.rule_set = rule_set if rule_set is not None else load_rule_set(self.settings.proposed_rules,
                                                                            self.settings.proposed_rules_file)
        self.gap = pd.Timedelta(minutes=self.settings.cycle_gap_minutes)
        self.objects: dict[tuple[str, str], ObjectState] = {}
        self.alerts: dict[str, Alert] = {}
        self.alert_cycles: dict[str, CycleState] = {}  # alert id -> its cycle, for the debug view
        self.closed_cycles: list[dict] = []
        self.keep_closed_cycles = keep_closed_cycles
        self.clock: pd.Timestamp | None = None  # latest reading time seen

    # ------------------------------------------------------------------ input
    def ingest(self, reading: Reading) -> list[Event]:
        """Process one reading from one object."""
        if reading.line is None or reading.object is None or pd.isna(reading.timestamp):
            return [Event(DATA_QUALITY, reading.timestamp, reading.line, reading.object,
                          detail={'issue': 'unidentified_reading',
                                  'message': 'Reading without line, object or timestamp was ignored'})]
        self.clock = reading.timestamp if self.clock is None else max(self.clock, reading.timestamp)
        key = (reading.line, reading.object)
        obj = self.objects.get(key)
        if obj is None:
            obj = self.objects[key] = ObjectState(line=reading.line, object=reading.object)

        events: list[Event] = []
        cycle = obj.current
        if cycle is not None and reading.timestamp - cycle.last_seen > self.gap:
            events += self._close(obj)
            cycle = None
        if cycle is None:
            cycle = self._start_cycle(obj, reading.timestamp)
            events.append(Event(CYCLE_STARTED, reading.timestamp, obj.line, obj.object, cycle.cycle_number,
                                detail={'referenceObject': cycle.reference_object}))

        events += self._add_reading(cycle, reading)
        obj.last_reading = reading
        events += self._evaluate(cycle, reading)
        return events

    def ingest_many(self, readings) -> list[Event]:
        events = []
        for reading in readings:
            events += self.ingest(reading)
        return events

    def close_idle(self, now: pd.Timestamp | None = None) -> list[Event]:
        """Close cycles whose object has been silent for more than the cycle gap."""
        now = now if now is not None else self.clock
        events = []
        if now is None:
            return events
        for obj in self.objects.values():
            if obj.current is not None and now - obj.current.last_seen > self.gap:
                events += self._close(obj)
        return events

    def flush(self) -> list[Event]:
        """Close every open cycle (end of a replay)."""
        events = []
        for obj in self.objects.values():
            if obj.current is not None:
                events += self._close(obj)
        return events

    def acknowledge(self, alert_id: str, at: pd.Timestamp | None = None, by: str | None = None) -> Alert:
        alert = self.alerts[alert_id]
        alert.acknowledged_at = at if at is not None else (self.clock or pd.Timestamp.now())
        alert.acknowledged_by = by
        return alert

    # ---------------------------------------------------------------- internals
    def _start_cycle(self, obj: ObjectState, at: pd.Timestamp) -> CycleState:
        reference_object = self.reference.match(obj.object)
        cycle = CycleState(line=obj.line, object=obj.object, object_key=object_key(obj.object),
                           reference_object=reference_object, cycle_number=obj.cycles_started,
                           limits=self.reference.limits_for(reference_object), start=at, last_seen=at)
        if reference_object is not None and self.rule_set.active:
            cycle.proposed = ProposedTracker(self.rule_set)
        obj.cycles_started += 1
        obj.current = cycle
        return cycle

    def _quality(self, cycle: CycleState, reading: Reading, issue: str, message: str) -> list[Event]:
        cycle.quality[issue] += 1
        if cycle.quality[issue] > 1:
            return []
        return [Event(DATA_QUALITY, reading.timestamp, cycle.line, cycle.object, cycle.cycle_number,
                      detail={'issue': issue, 'message': message})]

    def _add_reading(self, cycle: CycleState, reading: Reading) -> list[Event]:
        events: list[Event] = []
        if reading.timestamp < cycle.last_seen:
            events += self._quality(cycle, reading, 'out_of_order', 'Reading arrived out of time order')
        cycle.readings.append(reading)
        cycle.start = min(cycle.start, reading.timestamp)
        cycle.last_seen = max(cycle.last_seen, reading.timestamp)
        if reading.recipe is not None:
            cycle.recipes.add(reading.recipe)

        # Record-level versions of the notebook's Load and Cycles checks
        for name, value, expected in (('Recipe', reading.recipe, EXPECTED_RECIPES),
                                      ('Activity', reading.activity, ACTIVITIES),
                                      ('Step', reading.step, EXPECTED_STEPS)):
            if value is None:
                events += self._quality(cycle, reading, f'missing_{name.lower()}', f'{name} is empty')
            elif value not in expected:
                events += self._quality(cycle, reading, f'unknown_{name.lower()}', f'Unknown {name}: {value}')
        for column, (low, high) in VALID_RANGES.items():
            value = reading.value(column)
            if not is_missing(value) and not low <= value <= high:
                events += self._quality(cycle, reading, f'out_of_range:{column}',
                                        f'{column} = {value:g} is outside {low} to {high}')

        counter = reading.time_in_seconds
        if is_missing(counter):
            events += self._quality(cycle, reading, 'missing_counter', 'Time In Seconds is empty')
        else:
            if counter < 0:
                events += self._quality(cycle, reading, 'negative_counter', 'Elapsed time is negative')
            if cycle.last_counter is not None and counter < cycle.last_counter:
                events += self._quality(cycle, reading, 'counter_restart',
                                        'Elapsed counter restarted: this cycle may hold more than one CIP run')
            cycle.last_counter = counter
            if reading.activity is not None:
                span = cycle.counter_range.get(reading.activity)
                if span is None:
                    cycle.counter_range[reading.activity] = [counter, counter]
                else:
                    span[0] = min(span[0], counter)
                    span[1] = max(span[1], counter)

        if reading.activity is not None and reading.activity != cycle.current_activity:
            if reading.activity in cycle.activity_order:
                events += self._quality(cycle, reading, 'activity_repeated',
                                        f'{reading.activity} occurs more than once in this cycle')
            else:
                cycle.activity_order.append(reading.activity)
            cycle.current_activity = reading.activity
            events.append(Event(ACTIVITY_STARTED, reading.timestamp, cycle.line, cycle.object, cycle.cycle_number,
                                detail={'activity': reading.activity, 'recipe': reading.recipe}))
        return events

    def _evaluate(self, cycle: CycleState, reading: Reading) -> list[Event]:
        """Duration rule for the activity this reading belongs to, then the proposed T/A/C rules."""
        fired = cycle.proposed.update(reading) if cycle.proposed is not None else []
        if cycle.proposed is not None:   # proposed alerts: time below the limit grows while the streak lasts
            for rule_id, start in cycle.proposed.open_streak.items():
                alert = cycle.alerts.get(rule_id)
                if alert is not None:
                    alert.actual_minutes = (reading.timestamp - start).total_seconds() / 60
        events = self._evaluate_duration(cycle, reading)
        if self.rule_set.alerting:
            events += [self._raise_proposed(cycle, reading, f) for f in fired]
        return events

    def _proposed_view(self, cycle: CycleState, reading: Reading) -> dict | None:
        return cycle.proposed.view(reading) if cycle.proposed is not None else None

    def _raise_proposed(self, cycle: CycleState, reading: Reading, fired: Fired) -> Event:
        rule, phase = fired.rule, fired.rule.activity
        threshold = cycle.limits.get(phase, NAN)
        tolerance = self.settings.duration_tolerance
        alert = Alert(
            id=f'{cycle.line}:{cycle.object}:{cycle.cycle_number}:{phase}:{rule.id}',
            line=cycle.line, object=cycle.object, reference_object=cycle.reference_object,
            cycle_number=cycle.cycle_number, phase=phase, alert_type=f'{phase} {rule.parameter.lower()} (proposed)',
            raised_at=reading.timestamp, step_at_raise=reading.step, actual_minutes=fired.minutes,
            threshold_minutes=rule.minutes, limit_minutes=rule.minutes, tolerance=0.0,
            rule=f'Proposed {rule.id}: {rule.text}',
            tact_at_raise=tact_panel(reading.signals(), phase, cycle.duration_minutes(phase), threshold, tolerance,
                                     self._proposed_view(cycle, reading)),
            parameter=rule.parameter, rule_id=rule.id, proposed=True, measured=measured_for(rule, reading, fired),
            unit='minutes below limit',
        )
        cycle.alerts[rule.id] = alert
        self.alerts[alert.id] = alert
        self.alert_cycles[alert.id] = cycle
        return Event(ALERT_RAISED, reading.timestamp, cycle.line, cycle.object, cycle.cycle_number, alert=alert)

    def _evaluate_duration(self, cycle: CycleState, reading: Reading) -> list[Event]:
        phase = reading.activity
        if not cycle.has_reference or phase not in DURATION_THRESHOLDS:
            return []
        duration = cycle.duration_minutes(phase)
        existing = cycle.alerts.get(phase)
        if existing is not None:
            existing.actual_minutes = duration
            return []
        threshold = cycle.limits.get(phase, NAN)
        tolerance = self.settings.duration_tolerance
        if not is_duration_breach(duration, threshold, tolerance):
            return []
        alert = Alert(
            id=f'{cycle.line}:{cycle.object}:{cycle.cycle_number}:{phase}',
            line=cycle.line, object=cycle.object, reference_object=cycle.reference_object,
            cycle_number=cycle.cycle_number, phase=phase, alert_type=alert_type(phase),
            raised_at=reading.timestamp, step_at_raise=reading.step, actual_minutes=duration,
            threshold_minutes=threshold, limit_minutes=duration_limit(threshold, tolerance), tolerance=tolerance,
            rule=rule_text(phase, tolerance),
            tact_at_raise=tact_panel(reading.signals(), phase, duration, threshold, tolerance,
                                     self._proposed_view(cycle, reading)),
        )
        cycle.alerts[phase] = alert
        self.alerts[alert.id] = alert
        self.alert_cycles[alert.id] = cycle
        return [Event(ALERT_RAISED, reading.timestamp, cycle.line, cycle.object, cycle.cycle_number, alert=alert)]

    def _close(self, obj: ObjectState) -> list[Event]:
        cycle = obj.current
        cycle.end = cycle.last_seen
        for alert in cycle.alerts.values():
            if not alert.proposed:   # a proposed alert keeps its minutes below the limit
                alert.actual_minutes = cycle.duration_minutes(alert.phase)
            alert.cycle_closed_at = cycle.end
        row = cycle.summary(self.settings.duration_tolerance)
        flow_review = {
            flow: bool(is_flow_below_reference(row[f'{flow} Flow Actual (L/hr)'], row[FLOW_THRESHOLDS[flow]],
                                               self.settings.flow_tolerance))
            for flow in FLOWS
        }
        row['Flow Below Reference'] = flow_review  # monitoring only, never an alert
        self.closed_cycles.append(row)
        if self.keep_closed_cycles is not None:
            del self.closed_cycles[:-self.keep_closed_cycles]
        obj.last_closed = cycle
        obj.current = None
        return [Event(CYCLE_CLOSED, cycle.end, cycle.line, cycle.object, cycle.cycle_number,
                      detail={'status': row['Status'], 'alertCount': row['Alert Count'],
                              'recordCount': row['Record Count'], 'flowBelowReference': flow_review,
                              'alertIds': [a.id for a in cycle.alerts.values()]},
                      summary=row)]

    # ------------------------------------------------------------------ queries
    def cycle_for(self, alert: Alert) -> CycleState | None:
        if alert.id in self.alert_cycles:
            return self.alert_cycles[alert.id]
        obj = self.objects.get((alert.line, alert.object))
        if obj is None:
            return None
        for cycle in (obj.current, obj.last_closed):
            if cycle is not None and cycle.cycle_number == alert.cycle_number:
                return cycle
        return None

    def trend(self, alert_id: str, column: str) -> list[tuple[pd.Timestamp, float]]:
        """Values of one signal during the alert's activity (for the panel's trend lines)."""
        alert = self.alerts[alert_id]
        cycle = self.cycle_for(alert)
        if cycle is None:
            return []
        return [(r.timestamp, r.value(column)) for r in cycle.activity_readings(alert.phase)]

    def runs_in_progress(self, line: str | None = None) -> int:
        if self.clock is None:
            return 0
        return sum(1 for obj in self.objects.values()
                   if obj.current is not None and self.clock - obj.current.last_seen <= self.gap
                   and (line is None or obj.line == line))

    def overview(self, period: pd.Timedelta | None = pd.Timedelta(hours=24), line: str | None = None) -> dict:
        """Data for the overview page: summary band and one row per object.

        Alerts in the period stay listed until they age out; acknowledging only marks them.
        ``period=None`` keeps every alert; ``line`` limits the page to one CIP line.
        """
        since = None if self.clock is None or period is None else self.clock - period
        window = [a for a in self.alerts.values()
                  if (since is None or a.raised_at >= since) and (line is None or a.line == line)]
        window.sort(key=lambda a: a.raised_at, reverse=True)
        by_object: dict[tuple[str, str], list[Alert]] = {}
        for alert in window:
            by_object.setdefault((alert.line, alert.object), []).append(alert)

        rows = []
        for key, obj in sorted(self.objects.items()):
            if line is not None and obj.line != line:
                continue
            cycle = obj.current or obj.last_closed
            latest = obj.last_reading
            object_alerts = by_object.get(key, [])
            rows.append({
                'line': obj.line, 'objectId': obj.object,
                'referenceObject': cycle.reference_object if cycle else None,
                'running': obj.current is not None,
                'cycleNumber': cycle.cycle_number if cycle else None,
                'activity': latest.activity if latest else None, 'step': latest.step if latest else None,
                'lastReadingAt': latest.timestamp if latest else None,
                'latest': None if latest is None else {
                    'Temperature': latest.return_temp, 'Action': latest.return_flow,
                    'Concentration': latest.return_conductivity,
                    'Time': cycle.duration_minutes(latest.activity) if cycle and latest.activity else None,
                },
                'alerts': [a.to_dict() for a in object_alerts],
                'statusText': None if object_alerts else (
                    'No reference thresholds' if cycle and not cycle.has_reference
                    else 'Running' if obj.current is not None else 'Last run completed'),
            })
        rows.sort(key=lambda r: (not r['alerts'], -(pd.Timestamp(r['alerts'][0]['raisedAt']).value
                                                    if r['alerts'] else 0)))
        return {
            'clock': self.clock,
            'summary': {
                'openAlerts': len(window),
                'unacknowledgedAlerts': sum(not a.is_acknowledged for a in window),
                'objectsWithAlerts': len(by_object),
                'objectsTotal': len(rows),
                'runsInProgress': self.runs_in_progress(line),
            },
            'objects': rows,
        }

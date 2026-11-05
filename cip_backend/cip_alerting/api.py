"""HTTP API for the CIP Monitor UI and plant gateways. Uses only the standard library (no web framework).

The service runs in one of two modes (``CIP_MODE``):

- ``replay`` (default): a ``ReplayService`` thread replays the log files into the ``LiveMonitor`` as
  60-second pings, the way the IoT devices will.
- ``live``: no replay. Gateways push readings with ``POST /api/readings``; the clock is the latest reading.

Alerts, acknowledgements (an append-only audit log) and closed runs are kept in SQLite (``store.py``), so
acknowledgements survive a restart and a looped replay. The HTTP handler reads the monitor under a lock.

Endpoints
---------
GET  /                                 the overview page (web/index.html)
GET  /api/status                       mode, state and clock (live mode: last-reading age)
GET  /healthz                          liveness probe for container health checks ({"ok": true})
GET  /api/overview?period=24h&line=X   summary band and table rows (period: 24h, 7d, all; anything else is 400)
GET  /api/alerts/<id>                  debug panel: alert, TACT trends, cycle, data checks
POST /api/alerts/<id>/acknowledge      body {"by": "name", "note": "optional"}: "by" required, 1-64 characters;
                                       "note" at most 280. Idempotent: a repeat call returns the first
                                       acknowledgement unchanged. Stored in the audit log.
GET  /api/audit?limit=100&alert=<id>&line=&object=&event=a,b&facets=1
                                       audit log entries, newest first (limit 1-1000); facets = filter values
GET  /api/audit.csv?<same filters>     the audit log (or the filtered part) as CSV (oldest first), for quality teams
GET  /api/runs?object=X&line=Y&period=24h|7d|all&limit=&summary=0
                                       closed runs (run history), newest first (limit 1-1000), with activity
                                       times vs limits, flows and the run's alerts
GET  /api/readings/schema              JSON Schema of a reading upload
GET  /api/proposed-rules               proposed T/A/C rules (pending Danone sign-off) and the mode they run in
POST /api/readings                     live mode only: one reading or {"readings": [...]} (at most 5000).
                                       Needs "Authorization: Bearer <CIP_INGEST_TOKEN>" when that is set.
HEAD is answered for every GET path. Other methods get 405 JSON with an Allow header.
Unexpected errors return 500 JSON and are logged; a request body that stalls for 10 s is dropped (408).
"""
from __future__ import annotations

import hmac
import json
import logging
import re
import socket
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pandas as pd

from .config import ACTIVITIES, VALID_RANGES, Settings
from .ingest import MAX_BODY_BYTES, PayloadError, parse_payload, reading_schema
from .live import CycleState, LiveMonitor
from .models import ALERT_RAISED, CYCLE_CLOSED, Alert, Event, Reading, json_safe
from .notify import Notifier
from .rules import FLOW_THRESHOLDS, TACT_PARAMETERS, duration_limit, is_flow_below_reference
from .proposed_rules import RuleSet
from .simulator import load_readings, pings
from .store import MEMORY, Store
from .thresholds import DURATION_THRESHOLDS, FLOWS, load_reference

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parents[1] / 'web'
PERIODS = {'24h': pd.Timedelta(hours=24), '7d': pd.Timedelta(days=7), 'all': None}
MODES = ('replay', 'live')
ACTIVITY_LABELS = {'Pre Rinse': 'Pre-rinse', 'Caustic': 'Caustic wash', 'Mid Rinse': 'Mid rinse',
                   'Acid': 'Acid wash', 'Final Rinse': 'Final rinse', 'Final Drain': 'Final drain'}
OBJECT_NAME = re.compile(r'^\s*([A-Za-z0-9_]+)\s*-\s*(.+?)\s*$')
# Physically plausible range of each TACT reading (same limits as the data-quality checks), so the UI
# can show an impossible value (e.g. a negative flow) as an invalid reading instead of a real one.
PLAUSIBLE_RANGES = {'Temperature': VALID_RANGES['Return Temp'], 'Action': VALID_RANGES['Return Flow'],
                    'Concentration': VALID_RANGES['Return Conducivity']}
MAX_BY_LENGTH = 64
MAX_NOTE_LENGTH = 280
MAX_SMALL_BODY = 64 * 1024     # acknowledge bodies
MAX_LIST_LIMIT = 1000
STALE_AFTER_SECONDS = 180      # live mode: no reading for longer than this = stale feed
FUTURE_SLACK = pd.Timedelta(days=1)  # live mode: readings further ahead of the server clock are rejected
# Client-facing names for PLC signal columns (the PLC column 'Return Conducivity' is misspelt).
SIGNAL_LABELS = {'Return Temp': 'Return temperature', 'Return Flow': 'Return flow', 'Supply Flow': 'Supply flow',
                 'Return Conducivity': 'Return conductivity'}
QUALITY_TEXT = {
    'counter_restart': 'Elapsed counter restarted, so this cycle may hold more than one CIP run',
    'activity_repeated': 'An activity occurs more than once in this cycle',
    'out_of_order': 'Readings arrived out of time order',
    'missing_counter': 'Some readings have no elapsed counter',
    'negative_counter': 'Elapsed counter is negative on some readings',
}


def split_object(name: str) -> tuple[str, str]:
    """'C02C01-PMT TANK-1' -> ('C02C01', 'PMT TANK-1')."""
    match = OBJECT_NAME.match(name)
    return (match.group(1), match.group(2)) if match else (name, '')


def quality_text(issue: str) -> str:
    if issue in QUALITY_TEXT:
        return QUALITY_TEXT[issue]
    kind, _, column = issue.partition(':')
    if kind == 'out_of_range':
        return f'{SIGNAL_LABELS.get(column, column)} has readings outside the plausible sensor range'
    if kind.startswith('unknown_'):
        return f'Unknown {kind.removeprefix("unknown_")} name in some readings'
    if kind.startswith('missing_'):
        return f'{kind.removeprefix("missing_").capitalize()} is empty on some readings'
    return issue


class ModeError(RuntimeError):
    """The request needs the other mode (e.g. POST /api/readings while replaying)."""


class ReplayService:
    """Feeds a LiveMonitor and answers the API.

    ``mode='replay'``: replays the log files as 60-second pings. Readings before ``start_at`` are processed
    without waiting (warm-up), then the clock runs at ``speed`` x real time (60 = one simulated minute per second).

    ``mode='live'``: nothing is replayed; ``ingest()`` takes readings pushed by gateways (POST /api/readings).
    A background tick closes cycles of objects that went silent, using the wall-clock time since the last reading.

    ``store`` keeps alerts, acknowledgements and closed runs; the default is an in-memory store.
    """

    def __init__(self, settings: Settings | None = None, speed: float = 60.0,
                 start_at: pd.Timestamp | None = None, max_wait_seconds: float = 5.0, loop: bool = False,
                 *, mode: str = 'replay', store: Store | None = None, ingest_token: str | None = None,
                 stale_after_seconds: float = STALE_AFTER_SECONDS, tick_seconds: float = 15.0,
                 notifier: Notifier | None = None):
        if mode not in MODES:
            raise ValueError(f'mode must be one of {MODES}, not {mode!r}')
        self.settings = settings or Settings()
        self.mode = mode
        self.lock = threading.RLock()
        self.store = store if store is not None else Store(MEMORY)
        self.monitor = LiveMonitor(load_reference(self.settings.reference_file, self.settings.object_aliases), self.settings)
        self.readings = load_readings(self.settings) if mode == 'replay' else []
        self.data_start = self.readings[0].timestamp if self.readings else None
        self.data_end = self.readings[-1].timestamp if self.readings else None
        # Every object in the replayed logs and whether the reference workbook has thresholds for it, so the
        # UI can tell 'not monitored' from 'not reported yet' (the monitor only knows objects that have reported).
        # In live mode the objects are the ones that have reported so far.
        self._reference_match: dict[tuple[str, str], bool] = {
            key: self.monitor.reference.match(key[1]) is not None
            for key in {(r.line, r.object) for r in self.readings if r.line is not None and r.object is not None}}
        self.instance = uuid.uuid4().hex[:12]   # changes on every server start, so the UI can spot a restart
        self.speed = speed
        self.start_at = start_at
        self.max_wait_seconds = max_wait_seconds
        self.loop = loop            # start the replay again when the logs run out (for always-on demos)
        self.replay_count = 0
        self.ingest_token = ingest_token or None
        self.stale_after_seconds = stale_after_seconds
        self.tick_seconds = tick_seconds
        self.last_received: float | None = None   # wall clock (time.time()) of the last accepted reading
        self.readings_accepted = 0
        self._state = 'starting' if mode == 'replay' else 'waiting'
        # Teams / email for newly raised live alerts (notify.py); None or unconfigured = off.
        self.notifier = notifier if notifier is not None else Notifier()
        if self.notifier.store is None:
            self.notifier.store = self.store
        self._stop = threading.Event()
        target = self._run if mode == 'replay' else self._tick_loop
        self._thread = threading.Thread(target=target, name=f'cip-{mode}', daemon=True)

    def start(self) -> 'ReplayService':
        self.store.log_event('service_started', actor='system', plant_time=self.monitor.clock,
                             details={'mode': self.mode, 'instance': self.instance,
                                      'persistent': self.store.persistent})
        self.notifier.start()
        self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the replay or idle-check thread and write buffered rows."""
        self._stop.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout)
        self.notifier.stop(timeout)
        with self.lock:
            self.store.flush()

    # ------------------------------------------------------------ state
    @property
    def known_objects(self) -> dict[tuple[str, str], bool]:
        if self.mode == 'live':
            for key in self.monitor.objects:
                if key not in self._reference_match:
                    self._reference_match[key] = self.monitor.reference.match(key[1]) is not None
        return self._reference_match

    @property
    def state(self) -> str:
        if self.mode == 'live':
            age = self.last_reading_age()
            if age is None:
                return 'waiting'
            return 'stale' if age > self.stale_after_seconds else 'live'
        return self._state

    @state.setter
    def state(self, value: str) -> None:
        self._state = value

    def last_reading_age(self, now: float | None = None) -> float | None:
        if self.last_received is None:
            return None
        return max(0.0, (time.time() if now is None else now) - self.last_received)

    # ------------------------------------------------------------ events -> store
    @staticmethod
    def _apply_ack(alert: Alert, ack: dict) -> None:
        alert.acknowledged_at = pd.Timestamp(ack['plantTime']) if ack.get('plantTime') else alert.raised_at
        alert.acknowledged_by = ack.get('by')
        alert.acknowledged_at_wall = ack.get('recordedAt')
        alert.acknowledgement_note = ack.get('note')

    def _record(self, events: list[Event], notify: bool = False) -> None:
        """Store new alerts (restoring an earlier acknowledgement) and closed runs. Call under self.lock.

        ``notify``: the events happened while live (not during the replay's fast-forward), so alerts seen for the
        first time are handed to the notifier."""
        for event in events:
            if event.kind == ALERT_RAISED and event.alert is not None:
                ack = self.store.ack_for(event.alert)
                if ack is not None:
                    self._apply_ack(event.alert, ack)
                first_time = self.store.record_alert(event.alert)
                if first_time:
                    self.notifier.submit(event.alert, mode=self.mode, live=notify, plant_clock=self.monitor.clock)
            elif event.kind == CYCLE_CLOSED:
                if event.summary is not None:
                    self.store.record_run(event.summary)
                for alert_id in event.detail.get('alertIds', ()):
                    alert = self.monitor.alerts.get(alert_id)
                    if alert is not None:
                        self.store.update_alert(alert)

    # ------------------------------------------------------------ replay mode
    def _run(self) -> None:
        while True:
            self._replay_once()
            if not self.loop or self.state == 'error' or self._stop.is_set():
                return
            if self._stop.wait(min(self.max_wait_seconds, 5)):
                return
            self._restart_replay()

    def _restart_replay(self) -> None:
        """Fresh monitor: the next replay starts from an empty history (acknowledgements come back from the store)."""
        with self.lock:
            self.monitor = LiveMonitor(self.monitor.reference, self.settings, rule_set=self.monitor.rule_set)
            self.replay_count += 1
            self.store.log_event('replay_restarted', actor='system',
                                 details={'pass': self.replay_count + 1, 'instance': self.instance})

    def _replay_once(self) -> None:
        interval = self.settings.sample_interval_seconds
        previous = None
        try:
            for slot, batch in pings(self.readings, interval):
                if self._stop.is_set():
                    break
                warming = self.start_at is not None and slot < self.start_at
                if not warming and previous is not None and self.speed:
                    gap = (slot - previous).total_seconds()
                    if self._stop.wait(min(gap / self.speed, self.max_wait_seconds)):
                        break
                with self.lock:
                    events = []
                    for reading in batch:
                        events += self.monitor.ingest(reading)
                    events += self.monitor.close_idle(slot + pd.Timedelta(seconds=interval))
                    self._record(events, notify=not warming)
                    if not warming:
                        self.store.flush()   # warm-up rows are written in large batches
                    self.state = 'warming_up' if warming else 'live'
                previous = slot
            with self.lock:
                self.store.flush()
            if not self._stop.is_set():
                self.state = 'finished'
        except Exception:  # keep the API up and report the failure
            logger.exception('Replay stopped')
            self.state = 'error'

    # ------------------------------------------------------------ live mode
    def ingest(self, readings: list[Reading]) -> dict:
        """Readings pushed by gateways, in order. Readings of one object must be in time order; a duplicate or
        earlier reading is rejected with a reason. Returns counts and a summary of the events."""
        if self.mode != 'live':
            raise ModeError('Readings can only be pushed in live mode (CIP_MODE=live); this server replays logs')
        interval = pd.Timedelta(seconds=self.settings.sample_interval_seconds)
        horizon = pd.Timestamp.now() + FUTURE_SLACK
        rejected, events = [], []
        accepted = 0
        with self.lock:
            for index, reading in enumerate(readings):
                obj = self.monitor.objects.get((reading.line, reading.object))
                last = obj.last_reading.timestamp if obj is not None and obj.last_reading is not None else None
                reason = None
                if last is not None and reading.timestamp == last:
                    reason, message = 'duplicate', f'a reading for this object at {last.isoformat()} was already accepted'
                elif last is not None and reading.timestamp < last:
                    reason, message = 'out_of_order', (f'earlier than the last accepted reading for this object '
                                                       f'({last.isoformat()}); send readings in time order')
                elif reading.timestamp > horizon:
                    reason, message = 'in_future', 'more than 1 day ahead of the server clock'
                if reason:
                    rejected.append({'index': index, 'line': reading.line, 'object': reading.object,
                                     'dateTime': reading.timestamp.isoformat(), 'reason': reason, 'message': message})
                    continue
                events += self.monitor.ingest(reading)
                accepted += 1
            if accepted:
                # One interval of grace: another object's reading for the same minute may still be on its way.
                events += self.monitor.close_idle(self.monitor.clock - interval)
                self.last_received = time.time()
                self.readings_accepted += accepted
            self._record(events, notify=True)
            self.store.flush()
            raised = [e.alert for e in events if e.kind == ALERT_RAISED]
            return {
                'accepted': accepted, 'rejected': len(rejected), 'rejections': rejected[:200],
                'events': dict(Counter(e.kind for e in events)),
                'alertsRaised': [{'id': a.id, 'line': a.line, 'objectId': a.object, 'phase': a.phase,
                                  'cycleNumber': a.cycle_number, 'raisedAt': a.raised_at,
                                  'acknowledged': a.is_acknowledged} for a in raised[:200]],
                'clock': self.monitor.clock,
            }

    def tick(self, now: float | None = None) -> list[Event]:
        """Live mode: close cycles of objects silent for longer than the cycle gap, estimating plant time as the
        last reading's time plus the wall-clock time since it arrived."""
        with self.lock:
            age = self.last_reading_age(now)
            if self.mode != 'live' or self.monitor.clock is None or age is None:
                return []
            estimate = (self.monitor.clock + pd.Timedelta(seconds=age)
                        - pd.Timedelta(seconds=self.settings.sample_interval_seconds))
            events = self.monitor.close_idle(estimate)
            self._record(events)
            self.store.flush()
            return events

    def _tick_loop(self) -> None:
        while not self._stop.wait(self.tick_seconds):
            try:
                self.tick()
            except Exception:
                logger.exception('Idle check failed')

    def flush_cycles(self) -> list[Event]:
        """Close every open cycle and store it (end of a hand-over or a test; not used while running)."""
        with self.lock:
            events = self.monitor.flush()
            self._record(events)
            self.store.flush()
            return events

    # ------------------------------------------------------------ payloads
    def status(self) -> dict:
        data = {'mode': self.mode, 'state': self.state, 'speed': self.speed if self.mode == 'replay' else None,
                'clock': self.monitor.clock, 'loop': self.loop and self.mode == 'replay',
                'replayCount': self.replay_count, 'instance': self.instance,
                'dataStart': self.data_start, 'dataEnd': self.data_end,
                'intervalSeconds': self.settings.sample_interval_seconds,
                'store': {'persistent': self.store.persistent},
                'proposedRules': self.monitor.rule_set.status(),
                'notifications': self.notifier.status(self.mode)}
        if self.mode == 'live':
            age = self.last_reading_age()
            data.update({
                'lastReadingAt': self.monitor.clock,
                'lastReceivedAt': None if self.last_received is None else
                datetime.fromtimestamp(self.last_received, timezone.utc).isoformat(timespec='seconds'),
                'lastReadingAgeSeconds': None if age is None else round(age, 1),
                'staleAfterSeconds': self.stale_after_seconds,
                'readingsAccepted': self.readings_accepted,
                'ingestAuth': self.ingest_token is not None,
            })
        return data

    def overview(self, period: str = '24h', line: str | None = None) -> dict:
        with self.lock:
            data = self.monitor.overview(PERIODS.get(period, PERIODS['24h']), line or None)
            for row in data['objects']:
                row['code'], row['name'] = split_object(row['objectId'])
                obj = self.monitor.objects.get((row['line'], row['objectId']))
                row['runStartedAt'] = obj.current.start if obj is not None and obj.current is not None else None
            known_objects = self.known_objects
            data['lines'] = sorted({key[0] for key in self.monitor.objects} | {key[0] for key in known_objects})
            known = [has_ref for key, has_ref in known_objects.items() if not line or key[0] == line]
            data['configured'] = {'objects': len(known), 'monitored': sum(known)}
            data['status'] = self.status()
            data['period'] = period if period in PERIODS else '24h'
            data['plausibleRanges'] = PLAUSIBLE_RANGES
            return data

    def alert_detail(self, alert_id: str, others_limit: int = 6) -> dict:
        """Debug panel data for one alert; ``others_limit`` caps the list of the object's other alerts."""
        with self.lock:
            alert = self.monitor.alerts[alert_id]
            cycle = self.monitor.cycle_for(alert)
            code, name = split_object(alert.object)
            others = sorted((a for a in self.monitor.alerts.values()
                             if a.line == alert.line and a.object == alert.object and a.id != alert.id),
                            key=lambda a: a.raised_at, reverse=True)
            return {
                'alert': {**alert.to_dict(), 'code': code, 'name': name},
                'trends': self._trends(cycle, alert.phase, self.monitor.rule_set) if cycle else {},
                'cycle': self._cycle(cycle) if cycle else None,
                'otherAlerts': [{'id': a.id, 'phase': a.phase, 'cycleNumber': a.cycle_number,
                                 'raisedAt': a.raised_at, 'acknowledgedAt': a.acknowledged_at,
                                 'parameter': a.parameter, 'proposed': a.proposed}
                                for a in others[:others_limit]],
                'otherAlertCount': len(others),
                # audit trail of this alert (raised, notifications, acknowledgement), oldest first; matched on
                # id *and* raised_at, so a reused id in live mode never shows another alert's history
                'history': self.store.audit(50, alert.id, alert_raised_at=alert.raised_at)[::-1],
                'plausibleRanges': PLAUSIBLE_RANGES,
                'activityLabels': ACTIVITY_LABELS,
                'status': self.status(),
            }

    def acknowledge(self, alert_id: str, by: str, note: str | None = None) -> dict:
        """Acknowledge an alert once (stored in the audit log); a repeat call returns the first acknowledgement."""
        with self.lock:
            alert = self.monitor.alerts[alert_id]
            if alert.acknowledged_at is not None:
                return alert.to_dict()
            ack, _ = self.store.acknowledge(alert, by, note, plant_time=self.monitor.clock,
                                            details={'instance': self.instance, 'mode': self.mode})
            self._apply_ack(alert, ack)
            return alert.to_dict()

    def audit(self, limit: int = 100, alert_id: str | None = None, *, line: str | None = None,
              object_id: str | None = None, events=None, facets: bool = False) -> dict:
        filters = {'line': line, 'object_id': object_id, 'events': events}
        data = {'entries': self.store.audit(limit, alert_id, **filters),
                'total': self.store.audit_count(alert_id, **filters), 'persistent': self.store.persistent}
        if facets:
            data['facets'] = self.store.audit_facets()
        return data

    def runs(self, object_id: str | None = None, line: str | None = None, limit: int = 100,
             period: str | None = None, summary: bool = True) -> dict:
        """Closed runs, newest first. In replay mode runs after the replay clock are hidden (a looped replay
        starts again before the runs the previous pass stored). ``period`` (24h, 7d, all) counts back from the
        monitor clock, like the overview.

        Each run carries its activity times against their limits, flow medians against the reference, and the
        alerts of that run that the monitor still holds (so the UI can open them). ``summary=False`` leaves out
        the full cycle summary row (the UI's run history polls without it)."""
        with self.lock:
            clock = self.monitor.clock
            until = clock if self.mode == 'replay' else None
            window = PERIODS.get(period or 'all')
            since = clock - window if window is not None and clock is not None else None
            by_cycle: dict[tuple, list[Alert]] = {}
            for alert in self.monitor.alerts.values():
                by_cycle.setdefault((alert.line, alert.object, alert.cycle_number), []).append(alert)
        runs = self.store.runs(object_id, line, limit, until=until, since=since)
        tolerance, flow_tolerance = self.settings.duration_tolerance, self.settings.flow_tolerance
        for run in runs:
            run['code'], run['name'] = split_object(run['objectId'])
            row = run['summary']
            start, end = pd.Timestamp(run['start']), pd.Timestamp(run['end']) if run['end'] else None
            run['durationMinutes'] = None if end is None else round((end - start).total_seconds() / 60, 1)
            run['proposedAlertCount'] = int(row.get('Proposed Alert Count') or 0)
            run['qualityIssues'] = sum((row.get('Data Quality') or {}).values())
            activities = []
            for activity in ACTIVITIES:
                minutes = row.get(activity)
                threshold = row.get(f'{activity} Threshold (min)') if activity in DURATION_THRESHOLDS else None
                activities.append({
                    'activity': activity, 'label': ACTIVITY_LABELS[activity], 'minutes': minutes,
                    'threshold': threshold, 'limit': duration_limit(threshold, tolerance) if threshold is not None else None,
                    'breach': bool(row.get(f'{activity} Breach', False)), 'ran': minutes is not None})
            run['activities'] = activities
            run['flows'] = [{'flow': flow, 'medianActual': row.get(f'{flow} Flow Actual (L/hr)'),
                             'reference': row.get(FLOW_THRESHOLDS[flow]),
                             'belowReference': bool((row.get('Flow Below Reference') or {}).get(flow, False))}
                            for flow in FLOWS]
            run['flowTolerance'], run['durationTolerance'] = flow_tolerance, tolerance
            run['alerts'] = [{'id': a.id, 'phase': a.phase, 'parameter': a.parameter, 'proposed': a.proposed,
                              'raisedAt': a.raised_at, 'acknowledgedAt': a.acknowledged_at,
                              'acknowledgedBy': a.acknowledged_by}
                             for a in sorted(by_cycle.get((run['line'], run['objectId'], run['cycleNumber']), ()),
                                             key=lambda a: a.raised_at)
                             if start <= a.raised_at and (end is None or a.raised_at <= end)]
            if not summary:
                del run['summary']
        return json_safe({'runs': runs, 'persistent': self.store.persistent, 'period': period or 'all'})

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _trends(cycle: CycleState, phase: str, rule_set: RuleSet | None = None) -> dict:
        readings = sorted(cycle.activity_readings(phase), key=lambda r: r.timestamp)
        set_points = {actual: sp for sp, actual, _ in TACT_PARAMETERS.get(phase, [])}

        def series(column):
            return [[r.timestamp, r.value(column)] for r in readings] if column else []

        def proposed(parameter):
            """Proposed limit while the rule is checked (its step), null elsewhere, so the line has gaps."""
            rule = rule_set.rule_for(phase, parameter) if rule_set is not None else None
            if rule is None or not cycle.has_reference:
                return None
            return {'ruleId': rule.id, 'limitText': rule.limit_text,
                    'limit': [[r.timestamp, float(rule.limit(r.value(rule.set_point)))
                               if r.step == rule.step and r.value(rule.set_point) == r.value(rule.set_point)
                               else None] for r in readings]}

        elapsed, low, high = [], None, None
        for r in readings:
            counter = r.time_in_seconds
            if counter == counter:  # not NaN
                low = counter if low is None else min(low, counter)
                high = counter if high is None else max(high, counter)
            elapsed.append([r.timestamp, None if low is None else (high - low) / 60])
        trends = {
            'Temperature': {'actual': series('Return Temp'), 'setPoint': series(set_points.get('Return Temp'))},
            'Action': {'actual': series('Return Flow'), 'setPoint': series(set_points.get('Return Flow'))},
            'Concentration': {'actual': series('Return Conducivity'),
                              'setPoint': series(set_points.get('Return Conducivity'))},
            'Time': {'actual': elapsed, 'setPoint': []},
        }
        for parameter in ('Temperature', 'Action', 'Concentration'):
            extra = proposed(parameter)
            if extra is not None:
                trends[parameter]['proposed'] = extra
        return trends

    def _cycle(self, cycle: CycleState) -> dict:
        tolerance = self.settings.duration_tolerance
        row = cycle.summary(tolerance)
        obj = self.monitor.objects.get((cycle.line, cycle.object))
        activities = []
        for activity in ACTIVITIES:
            threshold = row.get(f'{activity} Threshold (min)') if activity in DURATION_THRESHOLDS else None
            activities.append({
                'activity': activity, 'label': ACTIVITY_LABELS[activity], 'minutes': row[activity],
                'threshold': threshold,
                'limit': duration_limit(threshold, tolerance) if threshold is not None else None,
                'breach': bool(row.get(f'{activity} Breach', False)),
                'ran': activity in cycle.counter_range,
            })
        flows = []
        for flow in FLOWS:
            actual, reference = row[f'{flow} Flow Actual (L/hr)'], row[FLOW_THRESHOLDS[flow]]
            flows.append({'flow': flow, 'medianActual': actual, 'reference': reference,
                          'belowReference': bool(is_flow_below_reference(actual, reference,
                                                                          self.settings.flow_tolerance))})
        return {
            'cycleNumber': cycle.cycle_number, 'referenceObject': cycle.reference_object,
            'start': cycle.start, 'end': cycle.end, 'running': obj is not None and obj.current is cycle,
            'recordCount': row['Record Count'], 'recipes': row['Recipes'], 'status': row['Status'],
            'activityOrder': cycle.activity_order, 'currentActivity': cycle.current_activity,
            'activities': activities, 'flows': flows, 'flowTolerance': self.settings.flow_tolerance,
            'quality': [{'issue': issue, 'count': count, 'message': quality_text(issue)}
                        for issue, count in cycle.quality.items()],
        }


class ApiError(Exception):
    """An error reply: status, message and optional extra JSON fields."""

    def __init__(self, status: int, message: str, extra: dict | None = None, headers: dict | None = None):
        super().__init__(message)
        self.status, self.message, self.extra, self.headers = status, message, extra or {}, headers


def _limit(query: dict, default: int = 100) -> int:
    raw = query.get('limit')
    if raw is None or raw == '':
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ApiError(HTTPStatus.BAD_REQUEST, f'"limit" must be a whole number from 1 to {MAX_LIST_LIMIT}') from None
    if not 1 <= value <= MAX_LIST_LIMIT:
        raise ApiError(HTTPStatus.BAD_REQUEST, f'"limit" must be a whole number from 1 to {MAX_LIST_LIMIT}')
    return value


EVENT_NAME = re.compile(r'^[a-z_]{1,40}$')


def _audit_filters(query: dict) -> dict:
    """line, object and event (comma list of event names) filters of /api/audit and /api/audit.csv."""
    events = [e.strip() for e in (query.get('event') or '').split(',') if e.strip()]
    bad = [e for e in events if not EVENT_NAME.match(e)]
    if bad:
        raise ApiError(HTTPStatus.BAD_REQUEST, f'Invalid event name {bad[0]!r}: lower-case letters and _ only')
    return {'line': query.get('line') or None, 'object_id': query.get('object') or None, 'events': events or None}


def make_handler(service: ReplayService, web_dir: Path = WEB_DIR):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'CIPMonitor/0.2'
        timeout = 10  # seconds; a client that stalls mid-request is dropped instead of holding a thread
        ALLOW = 'GET, HEAD, POST'

        def log_message(self, fmt, *args):  # quieter console
            logger.debug(fmt, *args)

        head_only = False

        def _send(self, status: int, body: bytes, content_type: str, headers: dict | None = None) -> None:
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            if not self.head_only:
                self.wfile.write(body)

        def _json(self, payload, status: int = HTTPStatus.OK, headers: dict | None = None) -> None:
            body = json.dumps(json_safe(payload), ensure_ascii=False, default=str).encode('utf-8')
            self._send(status, body, 'application/json; charset=utf-8', headers)

        def _error(self, status: int, message: str, headers: dict | None = None, extra: dict | None = None) -> None:
            self._json({'error': message, **(extra or {})}, status, headers)

        def _not_allowed(self) -> None:
            self._error(HTTPStatus.METHOD_NOT_ALLOWED, f'Method {self.command} not allowed', {'Allow': self.ALLOW})

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed

        def _guarded(self, route) -> None:
            """Run a route; any unexpected exception becomes a 500 JSON reply instead of a dropped connection."""
            try:
                route()
            except ApiError as err:
                self._error(err.status, err.message, err.headers, err.extra)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except (TimeoutError, socket.timeout):  # body never arrived in full
                self.close_connection = True
                try:
                    self._error(HTTPStatus.REQUEST_TIMEOUT, 'Request body timed out')
                except OSError:
                    pass
            except Exception:
                logger.exception('Unhandled error for %s %s', self.command, self.path)
                try:
                    self._error(HTTPStatus.INTERNAL_SERVER_ERROR, 'Internal server error')
                except OSError:
                    pass

        def do_GET(self):
            self._guarded(self._get)

        def do_HEAD(self):
            self.head_only = True
            self._guarded(self._get)

        def do_POST(self):
            self._guarded(self._post)

        def _get(self):
            url = urlsplit(self.path)
            query = {key: values[-1] for key, values in parse_qs(url.query).items()}
            parts = [unquote(part) for part in url.path.strip('/').split('/') if part]
            try:
                if not parts or parts == ['index.html']:
                    page = (web_dir / 'index.html').read_bytes()
                    return self._send(HTTPStatus.OK, page, 'text/html; charset=utf-8')
                if parts == ['healthz']:
                    return self._json({'ok': True, 'state': service.state})
                if parts == ['api', 'status']:
                    with service.lock:
                        return self._json(service.status())
                if parts == ['api', 'overview']:
                    period = query.get('period', '24h')
                    if period not in PERIODS:
                        return self._error(HTTPStatus.BAD_REQUEST,
                                           f'Unknown period {period!r}; use one of: {", ".join(PERIODS)}')
                    return self._json(service.overview(period, query.get('line')))
                if len(parts) == 3 and parts[:2] == ['api', 'alerts']:
                    others = 500 if query.get('others') == 'all' else 6
                    return self._json(service.alert_detail(parts[2], others))
                if parts == ['api', 'audit']:
                    return self._json(service.audit(_limit(query), query.get('alert') or None,
                                                    facets=query.get('facets') in ('1', 'true'), **_audit_filters(query)))
                if parts == ['api', 'audit.csv']:
                    body = service.store.audit_csv(query.get('alert') or None,
                                                   **_audit_filters(query)).encode('utf-8-sig')  # BOM for Excel
                    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')
                    return self._send(HTTPStatus.OK, body, 'text/csv; charset=utf-8',
                                      {'Content-Disposition': f'attachment; filename="cip-audit-log-{stamp}.csv"'})
                if parts == ['api', 'runs']:
                    period = query.get('period') or 'all'
                    if period not in PERIODS:
                        return self._error(HTTPStatus.BAD_REQUEST,
                                           f'Unknown period {period!r}; use one of: {", ".join(PERIODS)}')
                    return self._json(service.runs(query.get('object') or None, query.get('line') or None,
                                                   _limit(query), period, summary=query.get('summary') != '0'))
                if parts == ['api', 'readings', 'schema']:
                    return self._json(reading_schema())
                if parts == ['api', 'proposed-rules']:
                    return self._json(service.monitor.rule_set.to_dict())
                return self._error(HTTPStatus.NOT_FOUND, 'Not found')
            except KeyError:
                return self._error(HTTPStatus.NOT_FOUND, 'Alert not found')

        def _read_json(self, max_bytes: int):
            """The request body parsed as JSON (an empty body is None)."""
            try:
                length = int(self.headers.get('Content-Length') or 0)
                if length < 0:
                    raise ValueError(length)
            except ValueError:
                self.close_connection = True
                raise ApiError(HTTPStatus.BAD_REQUEST, 'Invalid Content-Length') from None
            if length > max_bytes:
                self.close_connection = True   # the unread body must not be parsed as the next request
                raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f'Body too large (limit {max_bytes} bytes)')
            if not length:
                return None
            try:
                return json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise ApiError(HTTPStatus.BAD_REQUEST, 'Body must be JSON') from None

        def _post(self):
            parts = [unquote(part) for part in urlsplit(self.path).path.strip('/').split('/') if part]
            if len(parts) == 4 and parts[:2] == ['api', 'alerts'] and parts[3] == 'acknowledge':
                return self._acknowledge(parts[2])
            if parts == ['api', 'readings']:
                return self._readings()
            self.close_connection = True
            return self._error(HTTPStatus.NOT_FOUND, 'Not found')

        def _acknowledge(self, alert_id: str):
            body = self._read_json(MAX_SMALL_BODY)
            body = {} if body is None else body
            if not isinstance(body, dict):
                return self._error(HTTPStatus.BAD_REQUEST, 'Body must be a JSON object')
            by, note = body.get('by'), body.get('note')
            if by is not None and not isinstance(by, str):
                return self._error(HTTPStatus.BAD_REQUEST, '"by" must be a string')
            by = (by or '').strip()
            if not by:
                return self._error(HTTPStatus.BAD_REQUEST, '"by" is required: who is acknowledging the alert')
            if len(by) > MAX_BY_LENGTH:
                return self._error(HTTPStatus.BAD_REQUEST, f'"by" must be at most {MAX_BY_LENGTH} characters')
            if note is not None and not isinstance(note, str):
                return self._error(HTTPStatus.BAD_REQUEST, '"note" must be a string')
            note = (note or '').strip() or None
            if note is not None and len(note) > MAX_NOTE_LENGTH:
                return self._error(HTTPStatus.BAD_REQUEST, f'"note" must be at most {MAX_NOTE_LENGTH} characters')
            try:
                return self._json(service.acknowledge(alert_id, by, note))
            except KeyError:
                return self._error(HTTPStatus.NOT_FOUND, 'Alert not found')

        def _readings(self):
            if service.ingest_token is not None:
                header = self.headers.get('Authorization') or ''
                scheme, _, given = header.partition(' ')
                if scheme.lower() != 'bearer' or not hmac.compare_digest(given.strip().encode('utf-8'),
                                                                         service.ingest_token.encode('utf-8')):
                    self.close_connection = True
                    return self._error(HTTPStatus.UNAUTHORIZED, 'Missing or wrong bearer token',
                                       {'WWW-Authenticate': 'Bearer realm="cip-monitor"'})
            if service.mode != 'live':
                self.close_connection = True
                return self._error(HTTPStatus.CONFLICT, 'Readings can only be pushed in live mode (CIP_MODE=live); '
                                                        'this server replays the log files')
            content_type = (self.headers.get('Content-Type') or '').split(';')[0].strip().lower()
            if content_type != 'application/json':
                self.close_connection = True
                return self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, 'Content-Type must be application/json')
            body = self._read_json(MAX_BODY_BYTES)
            if body is None:
                return self._error(HTTPStatus.BAD_REQUEST, 'Body is empty; send one reading or {"readings": [...]}')
            try:
                readings, errors = parse_payload(body)
            except PayloadError as err:
                return self._error(HTTPStatus.BAD_REQUEST, str(err))
            if errors:
                bad = len({e['index'] for e in errors})
                return self._error(HTTPStatus.BAD_REQUEST,
                                   f'{bad} reading(s) failed validation; nothing was stored. Fix them and send '
                                   f'the request again.',
                                   extra={'accepted': 0, 'rejected': bad, 'errorCount': len(errors),
                                          'errors': errors[:200]})
            return self._json(service.ingest(readings))

    return Handler


def serve(service: ReplayService, host: str = '127.0.0.1', port: int = 8000) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(service))

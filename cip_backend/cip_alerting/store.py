"""Persistence and audit trail (SQLite, standard library only).

What is kept
------------
- ``alerts``     every alert as raised: id, object, cycle, activity, threshold / limit / actual minutes,
                 ``raised_at`` (plant time of the reading that crossed the limit) and ``created_at`` (wall clock,
                 UTC, first time this server saw it). Final minutes are updated when the cycle closes.
- ``audit_log``  append-only (UPDATE and DELETE are refused by triggers): ``alert_raised`` the first time an
                 alert is seen, ``acknowledged`` (actor, optional note), ``service_started``,
                 ``replay_restarted``. Each row has the wall-clock time (UTC) and the plant time.
- ``runs``       one row per closed cycle (the run history), with the full cycle summary as JSON.

An alert is identified by its id *and* its ``raised_at`` plant time. Replaying the same logs (``--loop`` or a
server restart) raises the same alert again at the same plant time, so its acknowledgement comes back. In live
mode cycle numbers restart at 0 after a server restart, so a new alert can reuse an old id; it has a different
``raised_at`` and is therefore never matched to the old acknowledgement.

Writes from the monitor are buffered and written in one transaction by ``flush()`` (the replay warm-up raises a
few hundred alerts and closes ~700 runs in about a second). Acknowledgements are written at once, before the API
answers. One connection is shared by every thread behind a lock; file databases use WAL mode.

If the database cannot be opened (read-only folder, bad path), the store logs a warning and keeps everything in
memory (``persistent`` is False), so the monitor still runs.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .models import Alert, json_safe

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MEMORY = ':memory:'
DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / 'state' / 'cip_monitor.db'
FLUSH_THRESHOLD = 2000  # buffered rows that force a write even during warm-up

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS alerts (
    id TEXT NOT NULL,
    raised_at TEXT NOT NULL,              -- plant time of the first reading over the limit
    line TEXT, object TEXT, reference_object TEXT, cycle_number INTEGER, phase TEXT, alert_type TEXT,
    step_at_raise TEXT,
    threshold_minutes REAL, limit_minutes REAL, tolerance REAL,
    actual_minutes_at_raise REAL, actual_minutes REAL,
    cycle_closed_at TEXT,                 -- plant time
    created_at TEXT NOT NULL,             -- wall clock (UTC) when first stored
    PRIMARY KEY (id, raised_at)
);
CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,            -- wall clock (UTC)
    plant_time TEXT,                      -- monitor clock at the time
    event TEXT NOT NULL,
    alert_id TEXT, alert_raised_at TEXT, line TEXT, object TEXT,
    actor TEXT,
    details TEXT NOT NULL DEFAULT '{}'    -- JSON
);
CREATE INDEX IF NOT EXISTS audit_by_alert ON audit_log (alert_id, seq);
CREATE UNIQUE INDEX IF NOT EXISTS one_ack_per_alert ON audit_log (alert_id, alert_raised_at)
    WHERE event = 'acknowledged';
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
    BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
    BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TABLE IF NOT EXISTS runs (
    line TEXT NOT NULL, object TEXT NOT NULL, cycle_number INTEGER NOT NULL, run_start TEXT NOT NULL,
    run_end TEXT, status TEXT, alert_count INTEGER, record_count INTEGER, recipes TEXT, reference_object TEXT,
    summary TEXT NOT NULL,                -- JSON: the full cycle summary row
    closed_at TEXT NOT NULL,              -- wall clock (UTC)
    PRIMARY KEY (line, object, cycle_number, run_start)
);
CREATE INDEX IF NOT EXISTS runs_by_end ON runs (run_end);
CREATE INDEX IF NOT EXISTS runs_by_object ON runs (object, run_end);
"""

AUDIT_CSV_COLUMNS = ['seq', 'recorded_at_utc', 'plant_time', 'event', 'alert_id', 'alert_raised_at', 'line',
                     'object', 'actor', 'note', 'details']


def wall_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def iso(value) -> str | None:
    """Plant timestamps as ISO text (the same form the API returns)."""
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    return str(value)


def _number(value):
    return None if value is None or value != value else float(value)


def _csv_cell(value) -> str:
    """Text for one CSV cell; a leading = + - @ is escaped so spreadsheets never run it as a formula."""
    text = '' if value is None else str(value)
    return "'" + text if text[:1] in ('=', '+', '-', '@', '\t', '\r') else text


class Store:
    def __init__(self, path: str | os.PathLike | None = MEMORY):
        self.requested_path = str(path or MEMORY)
        self.lock = threading.RLock()
        self._alerts: list[tuple] = []        # buffered rows
        self._alert_updates: list[tuple] = []
        self._runs: list[tuple] = []
        self._audit: list[tuple] = []
        self.warning: str | None = None
        try:
            self.conn = self._open(self.requested_path)
            self.path = self.requested_path
        except (sqlite3.Error, OSError) as exc:
            self.warning = (f'Cannot use the database at {self.requested_path} ({exc}); acknowledgements, the audit '
                            f'log and run history are kept IN MEMORY ONLY and are lost on restart.')
            logger.warning('!' * 78 + '\n%s\n' + '!' * 78, self.warning)
            self.conn = self._open(MEMORY)
            self.path = MEMORY
        self.persistent = self.path != MEMORY
        with self.lock:
            self._known_alerts = {tuple(row) for row in self.conn.execute('SELECT id, raised_at FROM alerts')}
            self._acks: dict[tuple[str, str], dict] = {}
            for row in self.conn.execute("SELECT * FROM audit_log WHERE event = 'acknowledged' ORDER BY seq"):
                self._acks.setdefault((row['alert_id'], row['alert_raised_at']), self._ack(row))

    @staticmethod
    def _open(path: str) -> sqlite3.Connection:
        if path != MEMORY:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False, timeout=10, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA busy_timeout = 10000')
            if path != MEMORY:
                conn.execute('PRAGMA journal_mode = WAL')
                conn.execute('PRAGMA synchronous = FULL')   # an acknowledgement is on disk before the API answers
            conn.executescript(SCHEMA)
            conn.execute('INSERT OR IGNORE INTO meta VALUES (?, ?)', ('schema_version', str(SCHEMA_VERSION)))
            version = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
            if int(version) != SCHEMA_VERSION:
                raise sqlite3.DatabaseError(f'unsupported schema version {version}')
        except BaseException:
            conn.close()
            raise
        return conn

    def close(self) -> None:
        with self.lock:
            self.flush()
            self.conn.close()

    def info(self) -> dict:
        return {'persistent': self.persistent, 'warning': self.warning}

    # ------------------------------------------------------------ monitor writes (buffered)
    def record_alert(self, alert: Alert, plant_time=None) -> bool:
        """Store a newly raised alert. Returns True the first time this (id, raised_at) is seen."""
        key = (alert.id, iso(alert.raised_at))
        with self.lock:
            if key in self._known_alerts:
                return False
            self._known_alerts.add(key)
            now = wall_now()
            self._alerts.append((
                alert.id, key[1], alert.line, alert.object, alert.reference_object, alert.cycle_number, alert.phase,
                alert.alert_type, alert.step_at_raise, _number(alert.threshold_minutes),
                _number(alert.limit_minutes), alert.tolerance, _number(alert.actual_minutes),
                _number(alert.actual_minutes), iso(alert.cycle_closed_at), now))
            self._audit.append((now, iso(plant_time if plant_time is not None else alert.raised_at),
                                'alert_raised', alert.id, key[1], alert.line, alert.object, 'monitor',
                                json.dumps(json_safe(self._raised_details(alert)))))
            self._maybe_flush()
            return True

    @staticmethod
    def _raised_details(alert: Alert) -> dict:
        details = {'phase': alert.phase, 'cycleNumber': alert.cycle_number,
                   'thresholdMinutes': alert.threshold_minutes, 'limitMinutes': alert.limit_minutes,
                   'actualMinutes': alert.actual_minutes}
        if alert.proposed:   # a proposed T/A/C rule (pending Danone sign-off), never an approved one
            m = alert.measured or {}
            details.update({'proposed': True, 'ruleId': alert.rule_id, 'parameter': alert.parameter,
                            'value': m.get('value'), 'limit': m.get('limit'), 'unit': m.get('unit')})
        return details

    def update_alert(self, alert: Alert) -> None:
        """Final minutes and cycle end, when the alert's cycle closes."""
        with self.lock:
            self._alert_updates.append((_number(alert.actual_minutes), iso(alert.cycle_closed_at),
                                        alert.id, iso(alert.raised_at)))
            self._maybe_flush()

    def record_run(self, row: dict) -> None:
        """One closed cycle (a row of ``LiveMonitor.closed_cycles``)."""
        summary = json_safe(row)
        with self.lock:
            self._runs.append((row['Line'], row['Object'], int(row['Cycle Number']), iso(row['Cycle Start']),
                               iso(row['Cycle End']), row.get('Status'), int(row.get('Alert Count') or 0),
                               int(row.get('Record Count') or 0), row.get('Recipes'), row.get('Reference Object'),
                               json.dumps(summary, default=str), wall_now()))
            self._maybe_flush()

    def _maybe_flush(self) -> None:
        if len(self._alerts) + len(self._alert_updates) + len(self._runs) + len(self._audit) >= FLUSH_THRESHOLD:
            self.flush()

    @property
    def pending(self) -> int:
        return len(self._alerts) + len(self._alert_updates) + len(self._runs) + len(self._audit)

    def flush(self) -> None:
        """Write every buffered row in one transaction."""
        with self.lock:
            if not self.pending:
                return
            alerts, updates, runs, audit = self._alerts, self._alert_updates, self._runs, self._audit
            self._alerts, self._alert_updates, self._runs, self._audit = [], [], [], []
            try:
                self.conn.execute('BEGIN')
                self.conn.executemany('INSERT OR IGNORE INTO alerts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', alerts)
                self.conn.executemany('UPDATE alerts SET actual_minutes = ?, cycle_closed_at = ? '
                                      'WHERE id = ? AND raised_at = ?', updates)
                self.conn.executemany('INSERT OR IGNORE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', runs)
                self.conn.executemany('INSERT INTO audit_log (recorded_at, plant_time, event, alert_id, '
                                      'alert_raised_at, line, object, actor, details) VALUES (?,?,?,?,?,?,?,?,?)',
                                      audit)
                self.conn.execute('COMMIT')
            except sqlite3.Error:
                if self.conn.in_transaction:
                    self.conn.execute('ROLLBACK')
                logger.exception('Could not write %d buffered rows to %s', len(alerts) + len(runs) + len(audit),
                                 self.path)

    # ------------------------------------------------------------ audit events and acknowledgements
    def log_event(self, event: str, actor: str | None = None, details: dict | None = None, plant_time=None,
                  alert: Alert | None = None) -> None:
        with self.lock:
            self.flush()
            self.conn.execute(
                'INSERT INTO audit_log (recorded_at, plant_time, event, alert_id, alert_raised_at, line, object, '
                'actor, details) VALUES (?,?,?,?,?,?,?,?,?)',
                (wall_now(), iso(plant_time), event, alert.id if alert else None,
                 iso(alert.raised_at) if alert else None, alert.line if alert else None,
                 alert.object if alert else None, actor, json.dumps(json_safe(details or {}), default=str)))

    @staticmethod
    def _ack(row) -> dict:
        details = json.loads(row['details'] or '{}')
        return {'by': row['actor'], 'recordedAt': row['recorded_at'], 'plantTime': row['plant_time'],
                'note': details.get('note')}

    def ack_for(self, alert: Alert) -> dict | None:
        """The stored acknowledgement of this alert (same id and raised_at), if any."""
        with self.lock:
            return self._acks.get((alert.id, iso(alert.raised_at)))

    def acknowledge(self, alert: Alert, by: str, note: str | None = None, plant_time=None,
                    details: dict | None = None) -> tuple[dict, bool]:
        """Record an acknowledgement (written to disk before returning).

        Returns ``(ack, created)``; a second call for the same alert returns the first acknowledgement.
        """
        key = (alert.id, iso(alert.raised_at))
        with self.lock:
            if key in self._acks:
                return self._acks[key], False
            self.flush()  # the alert row (if still buffered) goes in first
            now = wall_now()
            payload = {'note': note, 'phase': alert.phase, 'cycleNumber': alert.cycle_number, **(details or {})}
            try:
                self.conn.execute(
                    'INSERT INTO audit_log (recorded_at, plant_time, event, alert_id, alert_raised_at, line, object, '
                    'actor, details) VALUES (?,?,?,?,?,?,?,?,?)',
                    (now, iso(plant_time), 'acknowledged', alert.id, key[1], alert.line, alert.object, by,
                     json.dumps(json_safe(payload), default=str)))
            except sqlite3.IntegrityError:  # acknowledged by another process sharing this file
                row = self.conn.execute("SELECT * FROM audit_log WHERE event = 'acknowledged' AND alert_id = ? "
                                        "AND alert_raised_at = ?", key).fetchone()
                self._acks[key] = self._ack(row)
                return self._acks[key], False
            ack = {'by': by, 'recordedAt': now, 'plantTime': iso(plant_time), 'note': note}
            self._acks[key] = ack
            return ack, True

    # ------------------------------------------------------------ queries
    @staticmethod
    def _audit_entry(row) -> dict:
        return {'seq': row['seq'], 'recordedAt': row['recorded_at'], 'plantTime': row['plant_time'],
                'event': row['event'], 'alertId': row['alert_id'], 'alertRaisedAt': row['alert_raised_at'],
                'line': row['line'], 'objectId': row['object'], 'actor': row['actor'],
                'details': json.loads(row['details'] or '{}')}

    @staticmethod
    def _audit_where(alert_id: str | None = None, line: str | None = None, object_id: str | None = None,
                     events=None, alert_raised_at=None) -> tuple[str, list]:
        """SQL WHERE clause for the audit filters (all optional; ``events`` is a list of event names)."""
        where, args = [], []
        for column, value in (('alert_id', alert_id), ('line', line), ('object', object_id),
                              ('alert_raised_at', iso(alert_raised_at))):
            if value:
                where.append(f'{column} = ?')
                args.append(value)
        if events:
            events = list(events)
            where.append(f'event IN ({",".join("?" * len(events))})')
            args.extend(events)
        return (' WHERE ' + ' AND '.join(where)) if where else '', args

    def _audit_rows(self, alert_id: str | None, limit: int | None, newest_first: bool, **filters):
        clause, args = self._audit_where(alert_id, **filters)
        sql = 'SELECT * FROM audit_log' + clause + ' ORDER BY seq ' + ('DESC' if newest_first else 'ASC')
        if limit is not None:
            sql += ' LIMIT ?'
            args.append(int(limit))
        with self.lock:
            self.flush()
            return self.conn.execute(sql, args).fetchall()

    def audit(self, limit: int = 100, alert_id: str | None = None, *, line: str | None = None,
              object_id: str | None = None, events=None, alert_raised_at=None) -> list[dict]:
        """Audit entries, newest first. Filters: alert id, line, object, event names, the alert's raised_at."""
        return [self._audit_entry(row) for row in self._audit_rows(
            alert_id, limit, newest_first=True, line=line, object_id=object_id, events=events,
            alert_raised_at=alert_raised_at)]

    def audit_count(self, alert_id: str | None = None, *, line: str | None = None, object_id: str | None = None,
                    events=None) -> int:
        clause, args = self._audit_where(alert_id, line=line, object_id=object_id, events=events)
        with self.lock:
            self.flush()
            return self.conn.execute('SELECT COUNT(*) FROM audit_log' + clause, args).fetchone()[0]

    def audit_facets(self) -> dict:
        """Lines, objects and event names present in the audit log (for the UI's filters)."""
        with self.lock:
            self.flush()
            objects = self.conn.execute('SELECT DISTINCT line, object FROM audit_log WHERE object IS NOT NULL '
                                        'ORDER BY line, object').fetchall()
            events = [r[0] for r in self.conn.execute('SELECT DISTINCT event FROM audit_log ORDER BY event')]
        return {'objects': [{'line': r[0], 'objectId': r[1]} for r in objects], 'events': events}

    def audit_csv(self, alert_id: str | None = None, *, line: str | None = None, object_id: str | None = None,
                  events=None) -> str:
        """The whole audit log (or the filtered part) as CSV, oldest first (for quality teams)."""
        out = io.StringIO()
        writer = csv.writer(out, lineterminator='\r\n')
        writer.writerow(AUDIT_CSV_COLUMNS)
        for row in self._audit_rows(alert_id, None, newest_first=False, line=line, object_id=object_id,
                                    events=events):
            details = json.loads(row['details'] or '{}')
            note = details.pop('note', None) if row['event'] == 'acknowledged' else None
            writer.writerow([_csv_cell(v) for v in (
                row['seq'], row['recorded_at'], row['plant_time'], row['event'], row['alert_id'],
                row['alert_raised_at'], row['line'], row['object'], row['actor'], note,
                json.dumps(details, ensure_ascii=False) if details else '')])
        return out.getvalue()

    def runs(self, object_id: str | None = None, line: str | None = None, limit: int = 100,
             until: pd.Timestamp | None = None, since: pd.Timestamp | None = None) -> list[dict]:
        """Closed runs, newest first. ``until`` hides runs that end after that plant time (used in replay mode,
        where a looped replay starts again before runs stored by the previous pass)."""
        sql, where, args = 'SELECT * FROM runs', [], []
        if object_id:
            where.append('object = ?')
            args.append(object_id)
        if line:
            where.append('line = ?')
            args.append(line)
        if until is not None:
            where.append('run_end <= ?')
            args.append(iso(until))
        if since is not None:   # runs that ended at or after this plant time
            where.append('run_end >= ?')
            args.append(iso(since))
        if where:
            sql += ' WHERE ' + ' AND '.join(where)
        sql += ' ORDER BY run_end DESC, line, object LIMIT ?'
        args.append(int(limit))
        with self.lock:
            self.flush()
            rows = self.conn.execute(sql, args).fetchall()
        return [{'line': r['line'], 'objectId': r['object'], 'cycleNumber': r['cycle_number'], 'start': r['run_start'],
                 'end': r['run_end'], 'status': r['status'], 'alertCount': r['alert_count'],
                 'recordCount': r['record_count'], 'recipes': r['recipes'], 'referenceObject': r['reference_object'],
                 'closedAt': r['closed_at'], 'summary': json.loads(r['summary'])} for r in rows]

    def count(self, table: str) -> int:
        if table not in ('alerts', 'runs', 'audit_log'):
            raise ValueError(table)
        with self.lock:
            self.flush()
            return self.conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]


def open_store(path: str | os.PathLike | None = None) -> Store:
    """Store at ``path``, else $CIP_DB_PATH, else <cip_backend>/state/cip_monitor.db."""
    if path is None:
        path = os.environ.get('CIP_DB_PATH', '').strip() or DEFAULT_DB_PATH
    return Store(path)

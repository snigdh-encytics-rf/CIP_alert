"""Persistence and audit trail: SQLite store, acknowledgements that survive a restart and a looped replay."""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cip_alerting import Alert, Settings  # noqa: E402
from cip_alerting.store import AUDIT_CSV_COLUMNS, MEMORY, Store, open_store  # noqa: E402

SETTINGS = Settings()
needs_data = pytest.mark.skipif(not (SETTINGS.log_files and SETTINGS.reference_file.exists()),
                                reason='datasets/ not available')


def _alert(cycle=3, raised='2026-07-01 10:04') -> Alert:
    return Alert(id=f'M11W01:C02C01-PMT TANK-1:{cycle}:Caustic', line='M11W01', object='C02C01-PMT TANK-1',
                 reference_object='M11W01_03_C02C01_PMT-1', cycle_number=cycle, phase='Caustic',
                 alert_type='Caustic duration', raised_at=pd.Timestamp(raised), step_at_raise='Circulation',
                 actual_minutes=4.0, threshold_minutes=3.0, limit_minutes=3.3, tolerance=0.1,
                 rule='Caustic duration > Caustic threshold x 1.1', tact_at_raise=[])


# ----------------------------------------------------------------- unit tests
def test_store_keeps_acknowledgements_across_reopen(tmp_path):
    path = tmp_path / 'state' / 'cip.db'
    store = Store(path)
    assert store.persistent
    alert = _alert()
    assert store.record_alert(alert) is True
    assert store.record_alert(alert) is False            # the same alert again (replay) is stored once
    ack, created = store.acknowledge(alert, 'alice', 'Valve checked', plant_time=pd.Timestamp('2026-07-01 10:30'))
    assert created and ack['by'] == 'alice'
    again, created = store.acknowledge(alert, 'bob')
    assert not created and again['by'] == 'alice'        # the first acknowledgement wins
    store.close()

    reopened = Store(path)
    ack = reopened.ack_for(_alert())
    assert ack['by'] == 'alice' and ack['note'] == 'Valve checked' and ack['plantTime'] == '2026-07-01T10:30:00'
    # same id, different raised_at: a different alert (live mode restarts cycle numbers at 0)
    assert reopened.ack_for(_alert(raised='2026-07-20 08:00')) is None
    assert reopened.count('alerts') == 1
    assert [e['event'] for e in reopened.audit()] == ['acknowledged', 'alert_raised']
    reopened.close()


def test_audit_log_is_append_only(tmp_path):
    store = Store(tmp_path / 'cip.db')
    store.log_event('service_started', actor='system')
    with pytest.raises(sqlite3.DatabaseError, match='append-only'):
        store.conn.execute("UPDATE audit_log SET actor = 'mallory'")
    with pytest.raises(sqlite3.DatabaseError, match='append-only'):
        store.conn.execute('DELETE FROM audit_log')
    assert store.audit()[0]['actor'] == 'system'


def test_unwritable_path_falls_back_to_memory(tmp_path, caplog):
    blocker = tmp_path / 'not-a-folder'
    blocker.write_text('x')
    with caplog.at_level(logging.WARNING):
        store = Store(blocker / 'cip.db')                # parent is a file: cannot be created
    assert not store.persistent and store.path == MEMORY
    assert 'IN MEMORY ONLY' in caplog.text
    store.record_alert(_alert())
    assert store.acknowledge(_alert(), 'op')[1] is True  # still works, in memory


def test_open_store_reads_env(tmp_path, monkeypatch):
    monkeypatch.setenv('CIP_DB_PATH', str(tmp_path / 'env.db'))
    store = open_store()
    assert store.path == str(tmp_path / 'env.db') and store.persistent
    store.close()


def test_audit_csv_has_headers_and_escapes_formulas(tmp_path):
    store = Store(tmp_path / 'cip.db')
    alert = _alert()
    store.record_alert(alert)
    store.acknowledge(alert, '=HYPERLINK("x")', 'note, with "quotes"')
    text = store.audit_csv()
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == AUDIT_CSV_COLUMNS
    ack = next(r for r in rows[1:] if r[3] == 'acknowledged')
    assert ack[8] == '\'=HYPERLINK("x")'                 # a spreadsheet never runs it as a formula
    assert ack[9] == 'note, with "quotes"'
    assert json.loads(ack[10])['phase'] == 'Caustic'


def test_runs_are_stored_once_and_listed_newest_first(tmp_path):
    store = Store(tmp_path / 'cip.db')
    for n, start in enumerate(['2026-07-01 08:00', '2026-07-02 08:00']):
        row = {'Line': 'M11W01', 'Object': 'C02C01-PMT TANK-1', 'Cycle Number': n, 'Cycle Start': pd.Timestamp(start),
               'Cycle End': pd.Timestamp(start) + pd.Timedelta(hours=1), 'Status': 'OK', 'Alert Count': 0,
               'Record Count': 60, 'Recipes': 'Caustic', 'Reference Object': 'X', 'Caustic': float('nan')}
        store.record_run(row)
        store.record_run(row)                            # replayed again: ignored
    runs = store.runs()
    assert [r['cycleNumber'] for r in runs] == [1, 0]
    assert runs[0]['summary']['Caustic'] is None         # NaN stored as null
    assert [r['cycleNumber'] for r in store.runs(until=pd.Timestamp('2026-07-01 12:00'))] == [0]
    assert store.runs(object_id='nope') == []


# ------------------------------------------------------------ data tests
@needs_data
def test_acknowledgement_survives_a_looped_replay(tmp_path):
    from cip_alerting.api import ReplayService
    service = ReplayService(SETTINGS, speed=0, store=Store(tmp_path / 'cip.db'))
    service._replay_once()
    alert = next(iter(service.monitor.alerts.values()))
    first = service.acknowledge(alert.id, 'operator', 'Checked')
    assert first['acknowledgedBy'] == 'operator' and first['acknowledgementNote'] == 'Checked'

    service._restart_replay()                            # what --loop does at the end of the logs
    assert alert.id not in service.monitor.alerts
    service._replay_once()
    again = service.monitor.alerts[alert.id].to_dict()
    assert again['acknowledgedBy'] == 'operator' and again['acknowledgedAt'] == first['acknowledgedAt']
    assert again['acknowledgedAtWall'] == first['acknowledgedAtWall']
    assert sum(not a.is_acknowledged for a in service.monitor.alerts.values()) == 220
    store = service.store
    assert store.count('alerts') == 221                  # the second pass adds no duplicates
    assert len(store.runs(limit=1000)) == 697            # closed runs (2 cycles are still open at the end)
    events = [e['event'] for e in store.audit(limit=1000)]
    assert events.count('acknowledged') == 1 and events.count('alert_raised') == 221
    assert 'replay_restarted' in events


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class _Server:
    def __init__(self, db: Path, *args: str):
        self.port, self.db, self.args, self.proc = _free_port(), db, args, None
        self.base = f'http://127.0.0.1:{self.port}'

    def start(self, timeout=90):
        env = {**os.environ, 'CIP_DB_PATH': str(self.db)}
        self.proc = subprocess.Popen([sys.executable, str(ROOT / 'scripts' / 'serve.py'), '--port', str(self.port),
                                      '--speed', '10000000', *self.args], cwd=ROOT, env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        end = time.time() + timeout
        while time.time() < end:
            try:
                if self.get('/api/status')['state'] == 'finished':
                    return self
            except OSError:
                pass
            time.sleep(0.3)
        self.stop()
        raise TimeoutError('server did not finish its replay')

    def stop(self):
        if self.proc and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            self.proc.wait(10)

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return json.loads(r.read())

    def raw(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return r.headers, r.read()

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method='POST',
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())


@needs_data
def test_acknowledgement_survives_a_server_restart(tmp_path):
    server = _Server(tmp_path / 'cip.db').start()
    try:
        assert server.get('/api/status')['store'] == {'persistent': True}
        alert_id = next(a['id'] for o in server.get('/api/overview?period=all')['objects'] for a in o['alerts'])
        path = '/api/alerts/' + quote(alert_id, safe='')
        for bad in ({}, {'by': ''}, {'by': '   '}, {'by': 'qa', 'note': 'x' * 281}, {'by': 'qa', 'note': 5}):
            with pytest.raises(urllib.error.HTTPError) as err:
                server.post(path + '/acknowledge', bad)
            assert err.value.code == 400, bad
        acked = server.post(path + '/acknowledge', {'by': 'operator', 'note': 'Valve V12 checked'})
        assert acked['acknowledgedBy'] == 'operator'
        audit = server.get('/api/audit?alert=' + quote(alert_id, safe=''))
        assert [e['event'] for e in audit['entries']] == ['acknowledged', 'alert_raised']   # newest first
        assert audit['entries'][0]['details']['note'] == 'Valve V12 checked'
        server.stop()

        server.start()                                   # a new process, same database file
        detail = server.get(path)['alert']
        assert detail['acknowledgedBy'] == 'operator' and detail['acknowledgedAt'] == acked['acknowledgedAt']
        assert detail['acknowledgementNote'] == 'Valve V12 checked'
        assert server.get('/api/overview?period=all')['summary']['unacknowledgedAlerts'] == 220
        audit = server.get('/api/audit?limit=1000')
        assert [e['event'] for e in audit['entries']].count('acknowledged') == 1
        assert [e['event'] for e in audit['entries']].count('service_started') == 2
        assert [e['event'] for e in audit['entries']].count('alert_raised') == 221   # not logged again on restart

        headers, body = server.raw('/api/audit.csv')
        assert headers['Content-Type'].startswith('text/csv')
        assert 'attachment' in headers['Content-Disposition']
        rows = list(csv.reader(io.StringIO(body.decode('utf-8-sig'))))
        assert rows[0] == AUDIT_CSV_COLUMNS
        assert sum(r[3] == 'acknowledged' for r in rows[1:]) == 1

        runs = server.get('/api/runs?limit=5')['runs']
        assert len(runs) == 5 and runs[0]['end'] >= runs[-1]['end']
        one = server.get('/api/runs?object=' + quote(detail['objectId'], safe='') + '&limit=1000')['runs']
        assert one and {r['objectId'] for r in one} == {detail['objectId']}
        for bad in ('0', '1001', 'x'):
            with pytest.raises(urllib.error.HTTPError) as err:
                server.get('/api/runs?limit=' + bad)
            assert err.value.code == 400
    finally:
        server.stop()

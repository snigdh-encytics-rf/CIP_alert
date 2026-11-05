"""Device ingestion: POST /api/readings in live mode (validation, auth, ordering) and parity through HTTP."""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

from cip_alerting import Settings, run_batch  # noqa: E402
from cip_alerting.config import ACTIVITIES  # noqa: E402
from cip_alerting.ingest import MAX_READINGS_PER_REQUEST, PayloadError, parse_payload, reading_schema  # noqa: E402
from cip_alerting.store import Store  # noqa: E402

SETTINGS = Settings()
needs_data = pytest.mark.skipif(not (SETTINGS.log_files and SETTINGS.reference_file.exists()),
                                reason='datasets/ not available')
OBJECT = 'C02C01-PMT TANK-1'


def reading(minute: int, obj: str = OBJECT, **extra) -> dict:
    at = pd.Timestamp('2026-07-01 10:00:30') + pd.Timedelta(minutes=minute)
    return {'Date Time': at.isoformat(), 'Line': 'M11W01', 'Object': obj, 'Recipe': 'Caustic',
            'Activity': 'Caustic', 'Step': 'Circulation', 'Time In Seconds': minute * 60, 'Return Temp': 75.2,
            'Return Flow': 18000, 'Return Conducivity': None, **extra}


# ----------------------------------------------------------------- validation (no server)
def test_valid_payloads():
    readings, errors = parse_payload(reading(0))
    assert not errors and len(readings) == 1
    r = readings[0]
    assert r.timestamp == pd.Timestamp('2026-07-01 10:00:30') and r.line == 'M11W01' and r.return_temp == 75.2
    assert r.return_conductivity != r.return_conductivity   # null -> NaN, like an empty log cell
    readings, errors = parse_payload({'readings': [reading(0), reading(1, **{'Date Time': '2026-07-01 10:01:30'})]})
    assert not errors and len(readings) == 2


def test_per_field_errors():
    bad = {'Date Time': '2026-07-01T10:00:00Z', 'Line': '', 'Return Temp': '75', 'Flip': True,
           'Return Flow': float('nan'), 'Return Conductivity': 3, 'Excel Row': -1}
    _, errors = parse_payload({'readings': [reading(0), bad]})
    by_field = {e['field']: e['message'] for e in errors}
    assert {e['index'] for e in errors} == {1}
    assert 'UTC offset' in by_field['Date Time']
    assert by_field['Line'] == 'must not be empty'
    assert by_field['Object'] == 'is required'
    assert by_field['Return Temp'] == 'must be a number or null'
    assert by_field['Flip'] == 'must be a number or null'      # booleans are not numbers
    assert by_field['Return Flow'] == 'must be a finite number'
    assert 'unknown field' in by_field['Return Conductivity']   # the PLC spells it 'Conducivity'
    assert 'Excel Row' in by_field
    _, errors = parse_payload(reading(0, **{'Date Time': 'yesterday'}))
    assert errors[0]['field'] == 'Date Time' and 'ISO 8601' in errors[0]['message']


@pytest.mark.parametrize('body, message', [
    ([reading(0)], 'one reading object'),
    ({'readings': []}, 'empty'),
    ({'readings': {}}, 'must be an array'),
    ({'readings': [reading(0)], 'extra': 1}, 'unexpected top-level'),
    ({'readings': [reading(0)] * (MAX_READINGS_PER_REQUEST + 1)}, 'too many readings'),
])
def test_payload_shape_errors(body, message):
    with pytest.raises(PayloadError, match=message):
        parse_payload(body)


def test_schema_lists_every_field():
    fields = reading_schema()['$defs']['reading']['properties']
    assert {'Date Time', 'Line', 'Object', 'Return Conducivity', 'Time In Seconds'} <= set(fields)
    assert reading_schema()['$defs']['reading']['additionalProperties'] is False


# ----------------------------------------------------------------- HTTP (live service in this process)
def _start(service):
    from cip_alerting.api import serve
    server = serve(service, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f'http://127.0.0.1:{server.server_address[1]}'


def call(url, method='GET', body=None, token=None, raw: bytes | None = None, content_type='application/json'):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    headers = {'Content-Type': content_type}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def http_error(*args, **kwargs) -> tuple[int, dict]:
    with pytest.raises(urllib.error.HTTPError) as err:
        call(*args, **kwargs)
    return err.value.code, json.loads(err.value.read())


@pytest.fixture
def live(tmp_path):
    from cip_alerting.api import ReplayService
    service = ReplayService(SETTINGS, mode='live', store=Store(tmp_path / 'cip.db'), ingest_token='s3cret')
    server, base = _start(service)
    yield service, base
    server.shutdown()
    server.server_close()


@needs_data
def test_auth_mode_and_status(live):
    service, base = live
    status = call(base + '/api/status')
    assert status['mode'] == 'live' and status['state'] == 'waiting' and status['clock'] is None
    assert status['ingestAuth'] is True and status['lastReadingAgeSeconds'] is None
    assert call(base + '/api/overview')['objects'] == []
    url = base + '/api/readings'
    code, body = http_error(url, 'POST', reading(0))                      # no token
    assert code == 401 and 'bearer' in body['error'].lower()
    assert http_error(url, 'POST', reading(0), token='wrong')[0] == 401
    assert http_error(url, 'POST', reading(0), token='s3cret', content_type='text/plain')[0] == 415
    code, body = http_error(url, 'POST', {'readings': [reading(0), {'Line': 5}]}, token='s3cret')
    assert code == 400 and body['accepted'] == 0 and body['rejected'] == 1
    assert {e['field'] for e in body['errors']} >= {'Line', 'Object', 'Date Time'}
    assert all(e['index'] == 1 for e in body['errors'])
    assert service.readings_accepted == 0                                 # nothing stored from a bad request
    assert http_error(url, 'POST', raw=b'{nope', token='s3cret')[0] == 400
    ok = call(url, 'POST', reading(0), token='s3cret')
    assert ok['accepted'] == 1 and ok['rejected'] == 0 and ok['events'] == {'cycle_started': 1, 'activity_started': 1}
    status = call(base + '/api/status')
    assert status['state'] == 'live' and status['clock'] == '2026-07-01T10:00:30'
    assert 0 <= status['lastReadingAgeSeconds'] < 5
    assert call(base + '/api/readings/schema')['title'] == 'CIP Monitor reading upload'

    service.last_received = time.time() - 200                             # no reading for > 3 min
    assert call(base + '/api/status')['state'] == 'stale'


@needs_data
def test_body_size_limit(live):
    _, base = live
    port = int(base.rsplit(':', 1)[1])
    with socket.create_connection(('127.0.0.1', port), timeout=5) as sock:
        sock.sendall(b'POST /api/readings HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer s3cret\r\n'
                     b'Content-Type: application/json\r\nContent-Length: 99999999\r\n\r\n')
        assert b' 413 ' in sock.recv(4096).split(b'\r\n')[0] + b' '


@needs_data
def test_readings_must_be_in_time_order_per_object(live):
    service, base = live
    url = base + '/api/readings'
    first = call(url, 'POST', {'readings': [reading(0), reading(1), reading(0, obj='C03C01-PMT TANK-2')]},
                 token='s3cret')
    assert first['accepted'] == 3
    result = call(url, 'POST', {'readings': [
        reading(1),                                     # duplicate of the last accepted reading
        reading(0),                                     # earlier than the last accepted reading
        reading(2), reading(2),                         # the second copy is a duplicate within this batch
        reading(1, obj='C03C01-PMT TANK-2'),            # another object keeps its own order
        reading(0, **{'Date Time': '2099-01-01T00:00:00'}, obj='C04C01-PMT TANK-3'),
    ]}, token='s3cret')
    assert result['accepted'] == 2 and result['rejected'] == 4
    assert [(r['index'], r['reason']) for r in result['rejections']] == [
        (0, 'duplicate'), (1, 'out_of_order'), (3, 'duplicate'), (5, 'in_future')]
    assert service.monitor.clock == pd.Timestamp('2026-07-01 10:02:30')


@needs_data
def test_idle_cycles_close_on_wall_clock(live):
    service, base = live
    call(base + '/api/readings', 'POST', {'readings': [reading(m) for m in range(5)]}, token='s3cret')
    assert service.tick() == []                                  # just received: nothing to close
    assert service.tick(now=service.last_received + 30 * 60) == []  # gap (30 min) + 1 interval of grace
    assert [e.kind for e in service.tick(now=service.last_received + 32 * 60)] == ['cycle_closed']
    runs = call(base + '/api/runs')['runs']
    assert len(runs) == 1 and runs[0]['objectId'] == OBJECT and runs[0]['recordCount'] == 5


# ----------------------------------------------------------------- parity through the ingestion path
@needs_data
def test_pushing_every_reading_over_http_matches_batch(tmp_path, capsys):
    """scripts/push_readings.py sends all 38,943 log readings to a live-mode server in batches; the alerts and
    cycles are the batch pipeline's (221 alerts, 699 cycles)."""
    import push_readings
    from cip_alerting.api import ReplayService
    service = ReplayService(SETTINGS, mode='live', store=Store(tmp_path / 'cip.db'), ingest_token='tok')
    server, base = _start(service)
    try:
        started = time.perf_counter()
        assert push_readings.main(['--url', base, '--speed', '0', '--batch-size', '2500', '--token', 'tok',
                                   '--quiet']) == 0
        elapsed = time.perf_counter() - started
        out = capsys.readouterr().out
        assert '38,943 accepted, 0 rejected, 221 alerts raised' in out, out
        overview = call(base + '/api/overview?period=all')
        assert overview['summary']['openAlerts'] == 221 and overview['summary']['objectsTotal'] == 29
        assert overview['status']['readingsAccepted'] == 38_943

        batch = run_batch(SETTINGS)
        batch_alerts = {(r['Line'], r['Object'], r['Cycle Number'], r['Alert Type']): r['Actual Value']
                        for r in batch.alerts.to_dict('records')}
        live_alerts = {(a.line, a.object, a.cycle_number, a.alert_type): a.actual_minutes
                       for a in service.monitor.alerts.values()}
        assert live_alerts.keys() == batch_alerts.keys()
        for key, actual in batch_alerts.items():
            assert live_alerts[key] == pytest.approx(actual)

        service.flush_cycles()                           # the logs end mid-cycle for 2 objects
        live_cycles = pd.DataFrame(service.monitor.closed_cycles).sort_values(['Line', 'Object', 'Cycle Number'])
        batch_cycles = batch.cycle_results.sort_values(['Line', 'Object', 'Cycle Number'])
        assert len(live_cycles) == len(batch_cycles) == 699
        for col in ['Line', 'Object', 'Cycle Number', 'Cycle Start', 'Cycle End', 'Record Count', 'Status',
                    'Alert Count']:
            assert live_cycles[col].tolist() == batch_cycles[col].tolist(), col
        for col in [*ACTIVITIES, 'Supply Flow Actual (L/hr)', 'Return Flow Actual (L/hr)']:
            pd.testing.assert_series_equal(live_cycles[col].astype(float).reset_index(drop=True),
                                           batch_cycles[col].astype(float).reset_index(drop=True), check_names=False)
        assert service.store.count('runs') == 699 and service.store.count('alerts') == 221
        assert len(call(base + '/api/runs?limit=1000')['runs']) == 699
        assert elapsed < 60, f'pushing the logs took {elapsed:.0f} s'
    finally:
        server.shutdown()
        server.server_close()

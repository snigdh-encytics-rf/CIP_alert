"""HTTP API used by the UI: replay the logs, then call every endpoint over real HTTP."""
from __future__ import annotations

import json
import sys
import threading
import urllib.request
from pathlib import Path
from urllib.parse import quote

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cip_alerting import Settings  # noqa: E402
from cip_alerting.api import ReplayService, serve, split_object  # noqa: E402

SETTINGS = Settings()
needs_data = pytest.mark.skipif(not (SETTINGS.log_files and SETTINGS.reference_file.exists()),
                                reason='datasets/ not available')


def test_split_object():
    assert split_object('C02C01-PMT TANK-1') == ('C02C01', 'PMT TANK-1')
    assert split_object('M01W02_CC01 - Raw Milk Tanker') == ('M01W02_CC01', 'Raw Milk Tanker')
    assert split_object('Milk Reception') == ('Milk Reception', '')


@pytest.fixture(scope='module')
def base_url():
    service = ReplayService(SETTINGS, speed=0)
    service._run()  # replay everything synchronously
    assert service.state == 'finished'
    server = serve(service, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{server.server_address[1]}'
    server.shutdown()


def call(url, method='GET', body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read())


@needs_data
def test_endpoints(base_url):
    assert call(f'{base_url}/api/status')['state'] == 'finished'

    everything = call(f'{base_url}/api/overview?period=all')
    assert everything['summary']['openAlerts'] == 221  # same as the batch pipeline
    assert everything['summary']['objectsTotal'] == 29

    one_line = call(f'{base_url}/api/overview?period=all&line=M11W01')
    assert all(row['line'] == 'M11W01' for row in one_line['objects'])

    alert_id = next(a['id'] for row in everything['objects'] for a in row['alerts'])
    detail = call(f'{base_url}/api/alerts/{quote(alert_id, safe="")}')
    assert detail['alert']['id'] == alert_id
    assert {p['parameter'] for p in detail['alert']['tact']} == {'Temperature', 'Action', 'Concentration', 'Time'}
    assert detail['trends']['Time']['actual'] and detail['cycle']['activities']

    acked = call(f'{base_url}/api/alerts/{quote(alert_id, safe="")}/acknowledge', 'POST', {'by': 'tester'})
    assert acked['acknowledgedBy'] == 'tester' and acked['acknowledgedAt']
    after = call(f'{base_url}/api/overview?period=all')
    assert after['summary']['unacknowledgedAlerts'] == 220

    with pytest.raises(urllib.error.HTTPError) as missing:
        call(f'{base_url}/api/alerts/nope')
    assert missing.value.code == 404
    with urllib.request.urlopen(f'{base_url}/') as page:
        assert b'CIP Monitor' in page.read()


@needs_data
def test_replay_mode_refuses_pushed_readings_and_serves_history(base_url):
    request = urllib.request.Request(f'{base_url}/api/readings', method='POST',
                                     data=json.dumps({'Date Time': '2026-07-01T10:00:00', 'Line': 'L',
                                                      'Object': 'O'}).encode(),
                                     headers={'Content-Type': 'application/json'})
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(request)
    assert err.value.code == 409 and 'live mode' in json.loads(err.value.read())['error']
    status = call(f'{base_url}/api/status')
    assert status['mode'] == 'replay' and status['store'] == {'persistent': False}   # default: in-memory store
    runs = call(f'{base_url}/api/runs?limit=1000')['runs']
    assert len(runs) == 697 and all(r['end'] <= status['clock'] for r in runs)
    assert call(f'{base_url}/api/audit?limit=5')['entries'][0]['event'] in ('alert_raised', 'acknowledged')


def _get_raw(url):
    with urllib.request.urlopen(url) as response:
        return response.status, response.headers, response.read()


@needs_data
def test_audit_filters_facets_and_alert_history(base_url):
    overview = call(f'{base_url}/api/overview?period=all&line=M11W02')
    alert = next(a for row in overview['objects'] for a in row['alerts'] if not a['acknowledgedAt'])
    path = f'{base_url}/api/alerts/{quote(alert["id"], safe="")}'
    assert [e['event'] for e in call(path)['history']] == ['alert_raised']
    call(path + '/acknowledge', 'POST', {'by': 'Priya', 'note': 'Valve V-12 checked'})
    history = call(path)['history']                                  # oldest first, this alert only
    assert [e['event'] for e in history] == ['alert_raised', 'acknowledged']
    assert history[1]['actor'] == 'Priya' and history[1]['details']['note'] == 'Valve V-12 checked'

    acks = call(f'{base_url}/api/audit?event=acknowledged&limit=1000')
    assert acks['total'] == len(acks['entries']) >= 1 and {e['event'] for e in acks['entries']} == {'acknowledged'}
    one = call(f'{base_url}/api/audit?line=M11W02&object={quote(alert["objectId"])}&limit=1000&facets=1')
    assert one['entries'] and all(e['line'] == 'M11W02' and e['objectId'] == alert['objectId'] for e in one['entries'])
    assert {'line': 'M11W02', 'objectId': alert['objectId']} in one['facets']['objects']
    assert {'alert_raised', 'acknowledged'} <= set(one['facets']['events'])
    both = call(f'{base_url}/api/audit?event=acknowledged,alert_raised&limit=1')
    assert both['total'] == call(f'{base_url}/api/audit?event=alert_raised&limit=1')['total'] + acks['total']
    with pytest.raises(urllib.error.HTTPError) as bad:
        call(f'{base_url}/api/audit?event=DROP%20TABLE')
    assert bad.value.code == 400

    status, headers, body = _get_raw(f'{base_url}/api/audit.csv?event=acknowledged&line=M11W02')
    assert status == 200 and headers['Content-Type'].startswith('text/csv')
    lines = body.decode('utf-8-sig').strip().splitlines()
    assert len(lines) >= 2 and all(',acknowledged,' in line for line in lines[1:]) and 'Valve V-12 checked' in body.decode()


@needs_data
def test_runs_period_summary_and_alerts(base_url):
    full = call(f'{base_url}/api/runs?limit=1000&period=all')
    assert full['period'] == 'all' and len(full['runs']) == 697
    run = next(r for r in full['runs'] if r['status'] == 'ALERT' and r['alerts'])
    assert 'summary' in run and len(run['activities']) == 6 and len(run['flows']) == 2
    assert any(x['breach'] and x['minutes'] > x['limit'] for x in run['activities'])
    assert run['durationMinutes'] > 0 and all(a['raisedAt'] >= run['start'] for a in run['alerts'])
    lean = call(f'{base_url}/api/runs?limit=1000&period=24h&summary=0')['runs']
    clock = call(f'{base_url}/api/status')['clock']
    assert 0 < len(lean) < len(full['runs']) and all('summary' not in r for r in lean)
    assert all(r['end'] <= clock for r in lean)
    with pytest.raises(urllib.error.HTTPError) as bad:
        call(f'{base_url}/api/runs?period=month')
    assert bad.value.code == 400

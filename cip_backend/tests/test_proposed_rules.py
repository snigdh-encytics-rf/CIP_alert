"""Proposed T/A/C rules (pending Danone sign-off): config file, streak logic, live == batch, modes and the API.

With the default mode ('show') nothing changes the 221 notebook alerts; 'alert' adds proposed alerts that the
live monitor and the batch pipeline must raise identically.
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.request
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cip_alerting import LiveMonitor, Reading, Settings, Simulator, readings_from_logs, run_batch  # noqa: E402
from cip_alerting.api import ReplayService, serve  # noqa: E402
from cip_alerting.models import ALERT_RAISED  # noqa: E402
from cip_alerting.proposed_rules import (DEFAULT_RULES_FILE, SIGN_OFF, RuleFileError, evaluate_batch,  # noqa: E402
                                         load_rule_set)
from cip_alerting.thresholds import Reference  # noqa: E402

SETTINGS = Settings()
HAS_DATA = bool(SETTINGS.log_files) and SETTINGS.reference_file.exists()
needs_data = pytest.mark.skipif(not HAS_DATA, reason='datasets/ not available')
OBJ = 'C02C01-PMT TANK-1'


# ----------------------------------------------------------------- config file
def test_rule_file_is_a_proposal_covering_every_tact_parameter():
    rules = load_rule_set('show')
    raw = json.loads(DEFAULT_RULES_FILE.read_text(encoding='utf-8'))
    assert raw['status'] == 'proposed' and raw['signOff'] == SIGN_OFF
    assert len(rules.rules) == 9 and all(r.status == 'proposed' for r in rules.rules)
    for item in raw['rules']:   # the fields the proposal promises
        assert {'id', 'parameter', 'activity', 'condition', 'thresholds', 'rationale', 'status'} <= item.keys()
    # every (activity, parameter) pair the TACT mapping uses has exactly one proposed rule
    from cip_alerting.rules import TACT_PARAMETERS, TACT_SIGNALS
    signal_to_parameter = {v['actual']: k for k, v in TACT_SIGNALS.items()}
    wanted = {(activity, signal_to_parameter[actual]) for activity, items in TACT_PARAMETERS.items()
              for _, actual, _ in items}
    assert {(r.activity, r.parameter) for r in rules.rules} == wanted
    temp = rules.rule_for('Caustic', 'Temperature')
    assert temp.limit(80.0) == 75.0 and rules.rule_for('Caustic', 'Concentration').limit(55.0) == pytest.approx(49.5)
    assert 'more than 5 °C below its set point for 5 min' in temp.text


def test_modes_and_defaults(monkeypatch):
    monkeypatch.delenv('CIP_PROPOSED_RULES', raising=False)
    assert Settings().proposed_rules == 'show'                      # default: panel only, no alerts
    rules = load_rule_set()
    assert rules.mode == 'show' and rules.active and not rules.alerting
    assert load_rule_set('off').rules == [] and not load_rule_set('off').active
    assert load_rule_set('alert').alerting
    monkeypatch.setenv('CIP_PROPOSED_RULES', 'alert')
    assert Settings().proposed_rules == 'alert'
    with pytest.raises(ValueError, match='off, show, alert'):
        load_rule_set('loud')


@pytest.mark.parametrize('change, message', [
    ({'status': 'approved'}, 'status must be "proposed"'),
    ({'condition': 'above'}, 'condition must be one of'),
    ({'thresholds': {'percent': 150, 'minutes': 5}}, 'percent must be above 0'),
    ({'thresholds': {'percent': 90, 'minutes': -1}}, 'minutes must be a number'),
    ({'parameter': 'Pressure'}, 'parameter must be one of'),
    ({'signal': 'Supply Flow'}, 'is read from'),
])
def test_bad_rule_files_are_refused(tmp_path, change, message):
    raw = json.loads(DEFAULT_RULES_FILE.read_text(encoding='utf-8'))
    raw['rules'][4].update(change)   # P-A-CAUSTIC
    path = tmp_path / 'rules.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    with pytest.raises(RuleFileError, match=message):
        load_rule_set('show', path)


# ----------------------------------------------------------------- streak logic, live == batch on synthetic data
def _reference() -> Reference:
    thresholds = pd.DataFrame({'Object': ['M11W01_03_C02C01_PMT-1'], 'Object Key': ['C02C01'],
                               'Pre Rinse Threshold (min)': [30.0], 'Caustic Threshold (min)': [60.0],
                               'Acid Threshold (min)': [30.0], 'Final Rinse Total Time (min)': [30.0],
                               'Supply Flow Threshold (L/hr)': [1000.0], 'Return Flow Threshold (L/hr)': [1000.0]})
    return Reference(summary=thresholds, process=thresholds, thresholds=thresholds)


def _row(minute, temp, step='Circulation', obj=OBJ, activity='Caustic'):
    return {'Date Time': pd.Timestamp('2026-07-01 10:00') + pd.Timedelta(minutes=minute), 'Line': 'M11W01',
            'Object': obj, 'Recipe': 'Caustic', 'Activity': activity, 'Step': step, 'Time In Seconds': minute * 60.0,
            'Temp SP': 80.0, 'Return Temp': temp, 'Return Flow': 15000.0, 'Min Return Flow SP': 12600.0,
            'Return Conducivity': 70.0, 'Caustic SP': 55.0, 'Acid SP': 65.0}


# minute: temperature. Fill (0-1), circulation from minute 2: ramp-up inside the 5 min allowance (2-6) is
# ignored; 7-10 low (4 min: not enough); 11 invalid (sensor range) ends the streak; 12-17 low -> met at 17
# (5 min after 12); 18-19 low, 20 back to normal; a second low streak (22-30) in the same cycle: no 2nd alert.
TEMPS = {0: 20, 1: 30, 2: 40, 3: 50, 4: 60, 5: 70, 6: 72, 7: 70, 8: 70, 9: 70, 10: 70, 11: 130, 12: 74.9,
         13: 74, 14: 74, 15: 74, 16: 74, 17: 74, 18: 73, 19: 70, 20: 82, 21: 82,
         **{m: 60 for m in range(22, 31)}}


def _synthetic_rows():
    return [_row(m, t, step='Fill Circuit' if m < 2 else 'Circulation') for m, t in TEMPS.items()]


def test_live_streak_rule_on_synthetic_data():
    settings = Settings(proposed_rules='alert')
    monitor = LiveMonitor(_reference(), settings)
    raised = [e.alert for row in _synthetic_rows() for e in monitor.ingest(Reading.from_row(row))
              if e.kind == ALERT_RAISED]
    assert len(raised) == 1                                  # one alert per rule per cycle
    alert = raised[0]
    assert alert.proposed and alert.parameter == 'Temperature' and alert.rule_id == 'P-T-CAUSTIC'
    assert alert.raised_at == pd.Timestamp('2026-07-01 10:17')
    assert alert.measured['streakStart'] == pd.Timestamp('2026-07-01 10:12')
    assert alert.measured['limit'] == 75.0 and alert.measured['value'] == 74
    assert alert.actual_minutes == 7.0                       # below the limit 10:12-10:19, then back to normal
    data = alert.to_dict()
    assert data['ruleStatus'] == 'proposed' and data['signOff'] == SIGN_OFF and '(proposed rule)' in data['title']
    assert 'pending Danone sign-off' in data['explanation']
    tact = {p['parameter']: p for p in data['tact']}
    assert tact['Temperature']['status'] == 'not_evaluated'  # the official status never changes
    assert tact['Temperature']['proposed']['status'] == 'outside'
    assert tact['Time']['status'] == 'within_limit'


def test_batch_matches_live_on_synthetic_data():
    logs = pd.DataFrame(_synthetic_rows() + [_row(m, 60, obj='C99X99-NO REF') for m in range(20)])
    logs['Cycle Number'] = 0
    logs['Reference Object'] = logs['Object'].map({OBJ: 'M11W01_03_C02C01_PMT-1'})   # unmatched object: not checked
    batch = evaluate_batch(logs, load_rule_set('alert'))
    assert len(batch) == 1
    row = batch.iloc[0]
    assert row['Rule'] == 'P-T-CAUSTIC' and row['Raised At'] == pd.Timestamp('2026-07-01 10:17')
    assert row['Streak Start'] == pd.Timestamp('2026-07-01 10:12') and row['Minutes Below'] == 7.0


def test_show_mode_raises_nothing_but_feeds_the_panel():
    monitor = LiveMonitor(_reference(), Settings(proposed_rules='show'))
    rows = [_row(m, 70) for m in range(0, 80)]               # caustic for 79 min: over the 66 min time limit
    events = [e for row in rows for e in monitor.ingest(Reading.from_row(row))]
    alerts = [e.alert for e in events if e.kind == ALERT_RAISED]
    assert [a.parameter for a in alerts] == ['Time']          # only the notebook's rule raises
    tact = {p['parameter']: p for p in alerts[0].tact_at_raise}
    proposed = tact['Temperature']['proposed']
    assert proposed['status'] == 'outside' and proposed['limit'] == 75.0 and proposed['signOff'] == SIGN_OFF
    assert proposed['metAt'] == pd.Timestamp('2026-07-01 10:10')   # the rule was met (no alert in show mode)
    assert tact['Action']['proposed']['status'] == 'within'
    off = LiveMonitor(_reference(), Settings(proposed_rules='off'))
    alert = [e.alert for row in rows for e in off.ingest(Reading.from_row(row)) if e.kind == ALERT_RAISED][0]
    assert all('proposed' not in p for p in alert.tact_at_raise)


# ----------------------------------------------------------------- the July 2026 data
@pytest.fixture(scope='module')
def batch_alert_mode():
    return run_batch(replace(SETTINGS, proposed_rules='alert'))


@needs_data
def test_default_settings_keep_notebook_parity():
    """'show' (the default) loads the rules but the alert table is the notebook's 221."""
    result = run_batch(replace(SETTINGS, proposed_rules='show'))
    assert len(result.alerts) == 221 and len(result.cycle_results) == 699
    assert len(result.proposed_alerts) == 47                  # the projection, never in result.alerts


@needs_data
def test_live_equals_batch_for_proposed_alerts(batch_alert_mode):
    batch = batch_alert_mode
    assert len(batch.alerts) == 221                           # Time alerts unchanged in 'alert' mode
    monitor = LiveMonitor(batch.reference, replace(SETTINGS, proposed_rules='alert'))
    raw = batch.logs.drop(columns=['Cycle Number', 'Reference Object', 'Object Key'])
    events = Simulator(monitor, readings_from_logs(raw)).run(flush=True)
    proposed = [a for a in monitor.alerts.values() if a.proposed]
    duration = [a for a in monitor.alerts.values() if not a.proposed]
    assert len(duration) == 221 and len(monitor.closed_cycles) == 699
    assert sum(e.kind == ALERT_RAISED for e in events) == 221 + len(proposed)
    want = {(r['Line'], r['Object'], r['Cycle Number'], r['Rule']): r for r in batch.proposed_alerts.to_dict('records')}
    got = {(a.line, a.object, a.cycle_number, a.rule_id): a for a in proposed}
    assert len(want) == 47 and got.keys() == want.keys()
    for key, row in want.items():
        alert = got[key]
        assert alert.raised_at == row['Raised At'] and alert.step_at_raise == row['Step']
        assert alert.measured['value'] == row['Value'] and alert.measured['limit'] == pytest.approx(row['Limit'])
        assert alert.measured['streakStart'] == row['Streak Start']
        assert alert.actual_minutes == pytest.approx(row['Minutes Below'])
        assert alert.cycle_closed_at is not None and alert.raised_at <= alert.cycle_closed_at
    # the cycle status stays the notebook's (Time rule only); proposed alerts are counted separately
    closed = pd.DataFrame(monitor.closed_cycles)
    assert closed['Status'].value_counts().to_dict() == {'OK': 329, 'NO REFERENCE': 211, 'ALERT': 159}
    assert closed['Proposed Alert Count'].sum() == 47


@needs_data
def test_projected_counts_in_the_rule_file_match_the_data(batch_alert_mode):
    """The proposal document and the JSON quote these numbers; keep them honest."""
    counts = batch_alert_mode.proposed_alerts['Rule'].value_counts().to_dict()
    for rule in load_rule_set('show').rules:
        assert rule.projected_alerts == counts.get(rule.id, 0), rule.id


# ----------------------------------------------------------------- API
def _serve(mode):
    service = ReplayService(replace(SETTINGS, proposed_rules=mode), speed=0)
    service._run()
    server = serve(service, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return service, server, f'http://127.0.0.1:{server.server_address[1]}'


def _get(url):
    with urllib.request.urlopen(url) as response:
        return json.loads(response.read())


@needs_data
def test_api_show_mode():
    service, server, base = _serve('show')
    try:
        status = _get(f'{base}/api/status')
        assert status['proposedRules']['mode'] == 'show' and status['proposedRules']['raisesAlerts'] is False
        assert status['proposedRules']['signOff'] == SIGN_OFF
        rules = _get(f'{base}/api/proposed-rules')
        assert len(rules['definitions']) == 9 and all(d['status'] == 'proposed' for d in rules['definitions'])
        overview = _get(f'{base}/api/overview?period=all')
        assert overview['summary']['openAlerts'] == 221
        alerts = [a for row in overview['objects'] for a in row['alerts']]
        assert not any(a['proposed'] for a in alerts)
        caustic = next(a for a in alerts if a['phase'] == 'Caustic')
        detail = _get(f'{base}/api/alerts/{quote(caustic["id"], safe="")}')
        temp = next(p for p in detail['alert']['tact'] if p['parameter'] == 'Temperature')
        assert temp['status'] == 'not_evaluated' and temp['proposed']['ruleId'] == 'P-T-CAUSTIC'
        line = detail['trends']['Temperature']['proposed']
        assert line['ruleId'] == 'P-T-CAUSTIC' and any(v is not None for _, v in line['limit'])
    finally:
        server.shutdown()


@needs_data
def test_api_alert_mode_lists_proposed_alerts():
    service, server, base = _serve('alert')
    try:
        overview = _get(f'{base}/api/overview?period=all')
        alerts = [a for row in overview['objects'] for a in row['alerts']]
        proposed = [a for a in alerts if a['proposed']]
        assert overview['summary']['openAlerts'] == len(alerts) == 221 + len(proposed)
        assert len(proposed) == 47                            # the whole replay has run: every one is listed
        one = proposed[0]
        assert one['parameter'] in ('Temperature', 'Action', 'Concentration') and one['ruleStatus'] == 'proposed'
        detail = _get(f'{base}/api/alerts/{quote(one["id"], safe="")}')
        assert detail['alert']['measured']['ruleId'] == one['ruleId']
        audit = _get(f'{base}/api/audit?limit=1000')['entries']
        raised = [e for e in audit if e['event'] == 'alert_raised' and e['details'].get('proposed')]
        assert raised and all(e['details']['ruleId'] for e in raised)
    finally:
        server.shutdown()

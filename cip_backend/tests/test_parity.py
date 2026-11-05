"""Checks that the backend reproduces the notebook, and that live == batch.

Run from cip_backend/:  python -m pytest tests -q
The data tests are skipped when datasets/ is missing.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cip_alerting import LiveMonitor, Reading, Settings, Simulator, readings_from_logs, run_batch  # noqa: E402
from cip_alerting.config import ACTIVITIES  # noqa: E402
from cip_alerting.models import ALERT_RAISED  # noqa: E402
from cip_alerting.rules import is_duration_breach, tact_panel  # noqa: E402
from cip_alerting.thresholds import object_key  # noqa: E402

SETTINGS = Settings()
HAS_DATA = bool(SETTINGS.log_files) and SETTINGS.reference_file.exists()
needs_data = pytest.mark.skipif(not HAS_DATA, reason='datasets/ not available')


# ----------------------------------------------------------------- unit tests
def test_object_key():
    assert object_key('C02C01-PMT TANK-1') == 'C02C01'
    assert object_key('M11W01_03_C02C01_PMT-1') == 'C02C01'
    assert object_key(' Milk Reception ') == 'MILK RECEPTION'


def test_duration_rule():
    assert is_duration_breach(11.01, 10, 0.10)
    assert not is_duration_breach(11.0, 10, 0.10)        # equal to the limit is not a breach
    assert not is_duration_breach(50, math.nan, 0.10)    # no threshold, no alert
    assert not is_duration_breach(math.nan, 10, 0.10)


def test_tact_panel_never_reports_missing_data_as_within_limit():
    panel = {p['parameter']: p for p in tact_panel({'Return Temp': math.nan, 'Temp SP': 80.0}, 'Caustic', 30, 20, 0.1)}
    assert panel['Temperature']['status'] == 'no_data'
    assert panel['Time']['status'] == 'out_of_limit'
    rinse = {p['parameter']: p for p in tact_panel({'Return Temp': 40.0}, 'Pre Rinse', 5, 10, 0.1)}
    assert rinse['Temperature']['status'] == 'not_applicable'
    assert rinse['Time']['status'] == 'within_limit'


def _reading(minute, counter, activity, obj='C02C01-PMT TANK-1'):
    return Reading(timestamp=pd.Timestamp('2026-07-01 10:00') + pd.Timedelta(minutes=minute), line='M11W01',
                   object=obj, recipe='Caustic', activity=activity, step='Circulation', time_in_seconds=counter)


def test_live_cycle_split_and_alert_on_synthetic_data():
    from cip_alerting.thresholds import Reference
    thresholds = pd.DataFrame({'Object': ['M11W01_03_C02C01_PMT-1'], 'Object Key': ['C02C01'],
                               'Pre Rinse Threshold (min)': [2.0], 'Caustic Threshold (min)': [3.0],
                               'Acid Threshold (min)': [5.0], 'Final Rinse Total Time (min)': [5.0],
                               'Supply Flow Threshold (L/hr)': [1000.0], 'Return Flow Threshold (L/hr)': [1000.0]})
    monitor = LiveMonitor(Reference(summary=thresholds, process=thresholds, thresholds=thresholds))
    kinds = []
    for minute in range(6):  # caustic for 5 minutes: limit is 3.3 min, crossed at minute 4
        kinds += [e.kind for e in monitor.ingest(_reading(minute, minute * 60, 'Caustic'))]
    assert kinds.count(ALERT_RAISED) == 1
    alert = next(iter(monitor.alerts.values()))
    assert alert.raised_at == pd.Timestamp('2026-07-01 10:04')
    assert alert.actual_minutes == 5.0
    # 31-minute silence starts cycle 1
    events = monitor.ingest(_reading(36, 0, 'Pre Rinse'))
    assert [e.kind for e in events][:2] == ['cycle_closed', 'cycle_started']
    assert events[1].cycle_number == 1
    assert monitor.closed_cycles[0]['Status'] == 'ALERT'


# ------------------------------------------------------------ data tests
@pytest.fixture(scope='module')
def batch():
    return run_batch(SETTINGS)


@pytest.fixture(scope='module')
def live(batch):
    monitor = LiveMonitor(batch.reference, SETTINGS)
    raw_logs = batch.logs.drop(columns=['Cycle Number', 'Reference Object', 'Object Key'])
    events = Simulator(monitor, readings_from_logs(raw_logs)).run(flush=True)
    return monitor, events


@needs_data
def test_batch_matches_notebook(batch):
    assert len(batch.logs) == 38_943
    assert len(batch.cycle_results) == 699
    assert batch.cycle_results['Status'].value_counts().to_dict() == {'OK': 329, 'NO REFERENCE': 211, 'ALERT': 159}
    assert len(batch.alerts) == 221
    assert batch.alerts['Alert Type'].value_counts().to_dict() == {
        'Caustic duration': 112, 'Pre Rinse duration': 64, 'Final Rinse duration': 28, 'Acid duration': 17}
    summary = batch.validation.summary_table()
    assert summary['WARNING'].sum() == 18


@needs_data
def test_live_raises_the_same_alerts_as_batch(batch, live):
    monitor, events = live
    batch_alerts = {(r['Line'], r['Object'], r['Cycle Number'], r['Alert Type']): r['Actual Value']
                    for r in batch.alerts.to_dict('records')}
    live_alerts = {(a.line, a.object, a.cycle_number, a.alert_type): a.actual_minutes for a in monitor.alerts.values()}
    assert live_alerts.keys() == batch_alerts.keys()
    for key, actual in batch_alerts.items():
        assert live_alerts[key] == pytest.approx(actual)
    assert sum(e.kind == ALERT_RAISED for e in events) == len(batch_alerts)


@needs_data
def test_live_cycles_match_batch(batch, live):
    monitor, _ = live
    live_cycles = pd.DataFrame(monitor.closed_cycles).sort_values(['Line', 'Object', 'Cycle Number'])
    batch_cycles = batch.cycle_results.sort_values(['Line', 'Object', 'Cycle Number'])
    assert len(live_cycles) == len(batch_cycles)
    for col in ['Line', 'Object', 'Cycle Number', 'Cycle Start', 'Cycle End', 'Record Count', 'Recipes',
                'Status', 'Alert Count']:
        assert live_cycles[col].tolist() == batch_cycles[col].tolist(), col
    for col in [*ACTIVITIES, 'Supply Flow Actual (L/hr)', 'Return Flow Actual (L/hr)']:
        pd.testing.assert_series_equal(live_cycles[col].astype(float).reset_index(drop=True),
                                       batch_cycles[col].astype(float).reset_index(drop=True),
                                       check_names=False)


@needs_data
def test_alert_is_raised_no_later_than_cycle_end(live):
    monitor, _ = live
    for alert in monitor.alerts.values():
        assert alert.raised_at <= alert.cycle_closed_at
        assert alert.actual_minutes > alert.limit_minutes

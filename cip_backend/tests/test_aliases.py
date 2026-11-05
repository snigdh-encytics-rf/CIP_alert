"""Object-name aliases: workbook rows without a usable equipment code (RMT1, Milk Reception ...)."""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cip_alerting import LiveMonitor, Settings, Simulator, readings_from_logs, run_batch  # noqa: E402
from cip_alerting.config import ALIASES_FILE, default_object_aliases  # noqa: E402
from cip_alerting.thresholds import reference_object_for_log  # noqa: E402

ON = Settings(object_aliases_file=ALIASES_FILE)
HAS_DATA = bool(ON.log_files) and ON.reference_file.exists()
needs_data = pytest.mark.skipif(not HAS_DATA, reason='datasets/ not available')


def test_alias_is_only_a_fallback():
    thresholds = pd.DataFrame({'Object': ['M11W01_03_C02C01_PMT-1', 'RMT1'], 'Object Key': ['C02C01', 'RMT1']})
    aliases = {'C01C01-RMT Tank-1': 'RMT1', 'C02C01-PMT TANK-1': 'RMT1'}
    assert reference_object_for_log('C01C01-RMT Tank-1', thresholds, aliases) == 'RMT1'
    assert reference_object_for_log('C02C01-PMT TANK-1', thresholds, aliases) == 'M11W01_03_C02C01_PMT-1'
    assert pd.isna(reference_object_for_log('C01C01-RMT Tank-1', thresholds, None))


def test_env_switch(monkeypatch):
    monkeypatch.setenv('CIP_OBJECT_ALIASES', 'off')
    assert default_object_aliases() is None and Settings().object_aliases == {}
    monkeypatch.setenv('CIP_OBJECT_ALIASES', '')
    assert default_object_aliases() == ALIASES_FILE and len(Settings().object_aliases) == 7


@needs_data
def test_aliases_bring_every_object_under_monitoring_and_live_equals_batch():
    batch = run_batch(ON)
    cycles = batch.cycle_results
    assert len(cycles) == 699 and 'NO REFERENCE' not in set(cycles['Status'])
    assert len(batch.alerts) == 561
    monitor = LiveMonitor(batch.reference, ON)
    raw = batch.logs.drop(columns=['Cycle Number', 'Reference Object', 'Object Key'])
    Simulator(monitor, readings_from_logs(raw)).run(flush=True)
    assert len([a for a in monitor.alerts.values() if not a.proposed]) == 561
    assert pd.DataFrame(monitor.closed_cycles)['Status'].value_counts().to_dict() == \
        cycles['Status'].value_counts().to_dict()


@needs_data
def test_switching_aliases_off_restores_the_notebook():
    batch = run_batch(replace(ON, object_aliases_file=None))
    assert len(batch.alerts) == 221
    assert batch.cycle_results['Status'].value_counts().to_dict() == {'OK': 329, 'NO REFERENCE': 211, 'ALERT': 159}


def test_alias_to_unknown_workbook_object_is_refused(tmp_path):
    if not HAS_DATA:
        pytest.skip('datasets/ not available')
    from cip_alerting import load_reference
    with pytest.raises(ValueError, match='do not exist'):
        load_reference(ON.reference_file, {'C01C01-RMT Tank-1': 'No Such Row'})

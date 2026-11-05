"""Alert table (notebook section 6) and the per-cycle debug view (stakeholder walkthrough).

The walkthrough functions explain one alerting cycle: threshold comparison,
the moment each duration crossed its limit, the TACT values at that moment, and
raw step-level evidence. They power the debug panel of the UI.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .cycles import CYCLE_KEYS
from .rules import (ALERT_RULES, DURATION_UNIT, PARAMETER_PLOTS, TACT_PARAMETERS, duration_limit,
                    flow_limit, phase_of)
from .thresholds import DURATION_THRESHOLDS, FLOWS

ALERT_COLUMNS = CYCLE_KEYS + ['Cycle Start', 'Cycle End', 'Alert Type', 'Actual Value', 'Threshold', 'Unit']


def build_alerts(cycle_results: pd.DataFrame) -> pd.DataFrame:
    """One row per breach, with the actual value and the threshold it was compared with."""
    alerts = pd.concat([
        cycle_results.loc[cycle_results[breach], CYCLE_KEYS + ['Cycle Start', 'Cycle End', actual, threshold]]
        .rename(columns={actual: 'Actual Value', threshold: 'Threshold'})
        .assign(**{'Alert Type': name, 'Unit': unit})
        for name, breach, actual, threshold, unit in ALERT_RULES
    ]).sort_index(kind='stable').reset_index(drop=True)
    return alerts[ALERT_COLUMNS]


# ---------------------------------------------------------------------------
# Walkthrough / debug view for one cycle
# ---------------------------------------------------------------------------
def cycle_overview(cycle: pd.Series) -> pd.DataFrame:
    elapsed = (cycle['Cycle End'] - cycle['Cycle Start']).total_seconds() / 60
    return pd.DataFrame({'Value': [
        cycle['Line'], cycle['Object'], cycle['Reference Object'], cycle['Cycle Number'],
        cycle['Cycle Start'], cycle['Cycle End'], elapsed, cycle['Record Count'],
        cycle['Recipes'], cycle['Status'], cycle['Alert Count'],
    ]}, index=['Line', 'Log object', 'Reference object', 'Cycle number', 'Start time', 'End time',
               'Elapsed time (min)', 'Log records', 'Recipes', 'Status', 'Alert count'])


def threshold_comparison(cycle: pd.Series, duration_tolerance: float, flow_tolerance: float) -> pd.DataFrame:
    """Actual values versus alert limits for every signal of one cycle."""
    rows = []
    for phase in DURATION_THRESHOLDS:
        actual = cycle[phase]
        threshold = cycle[f'{phase} Threshold (min)']
        limit = duration_limit(threshold, duration_tolerance) if pd.notna(threshold) else np.nan
        rows.append({'Signal': f'{phase} duration', 'Role': 'Alert rule',
                     'Actual': actual, 'Threshold': threshold, 'Alert limit': limit, 'Unit': 'min',
                     'Result': 'ALERT' if pd.notna(actual) and pd.notna(limit) and actual > limit else 'OK'})
    for flow in FLOWS:
        actual = cycle[f'{flow} Flow Actual (L/hr)']
        threshold = cycle[f'{flow} Flow Threshold (L/hr)']
        limit = flow_limit(threshold, flow_tolerance) if pd.notna(threshold) else np.nan
        rows.append({'Signal': f'{flow} flow', 'Role': 'Monitoring only',
                     'Actual': actual, 'Threshold': threshold, 'Alert limit': limit, 'Unit': 'L/hr',
                     'Result': ('Below reference' if pd.notna(actual) and pd.notna(limit) and actual < limit
                                else 'Within reference')})
    return pd.DataFrame(rows)


def alert_instant(phase_records: pd.DataFrame, threshold: float, duration_tolerance: float):
    """The record closest to the moment a duration crossed its alert limit.

    Returns (record, info) or (None, None) when the activity has no records.
    """
    phase_records = phase_records.sort_values('Time In Seconds')
    if phase_records.empty:
        return None, None
    start_time = phase_records['Date Time'].iloc[0]
    start_counter = phase_records['Time In Seconds'].iloc[0]
    limit = duration_limit(threshold, duration_tolerance)
    crossing_counter = start_counter + limit * 60
    record = phase_records.loc[(phase_records['Time In Seconds'] - crossing_counter).abs().idxmin()]
    info = {'Calculated crossing time': start_time + pd.Timedelta(minutes=limit),
            'Selected log timestamp': record['Date Time'],
            'Duration threshold (min)': threshold,
            'Alert limit (min)': limit}
    return record, info


def tact_snapshot(record: pd.Series, phase: str) -> list[dict]:
    """TACT values against their set points in one record (notebook format)."""
    rows = []
    for set_point_col, actual_col, unit in TACT_PARAMETERS.get(phase, []):
        actual = pd.to_numeric(pd.Series([record[actual_col]]), errors='coerce').iloc[0]
        set_point = pd.to_numeric(pd.Series([record[set_point_col]]), errors='coerce').iloc[0]
        rows.append({'Alert phase': phase, 'Alert timestamp': record['Date Time'],
                     'TACT parameter': set_point_col, 'Actual value': actual, 'TACT set point': set_point,
                     'Actual minus set point': actual - set_point, 'Unit': unit, 'Source step': record['Step']})
    return rows


def tact_at_alerts(records: pd.DataFrame, cycle_alerts: pd.DataFrame, duration_tolerance: float):
    """TACT values at each duration-alert instant. Returns (alert_instants, tact_at_alert)."""
    instants, tact_rows = [], []
    duration_alerts = cycle_alerts[cycle_alerts['Alert Type'].str.endswith(' duration')]
    for _, alert in duration_alerts.iterrows():
        phase = phase_of(alert['Alert Type'])
        record, info = alert_instant(records[records['Activity'].eq(phase)], alert['Threshold'], duration_tolerance)
        if record is None:
            continue
        instants.append({'Alert phase': phase, **info, 'Observed duration (min)': alert['Actual Value']})
        tact_rows.extend(tact_snapshot(record, phase))
    return pd.DataFrame(instants), pd.DataFrame(tact_rows)


def step_evidence(records: pd.DataFrame) -> pd.DataFrame:
    """Raw step-level evidence: first/last timestamp, samples and counter per activity and step."""
    evidence = (records.groupby(['Activity', 'Step'], as_index=False)
                .agg(**{'First timestamp': ('Date Time', 'min'), 'Last timestamp': ('Date Time', 'max'),
                        'Samples': ('Date Time', 'size'), 'Start counter (sec)': ('Time In Seconds', 'min'),
                        'End counter (sec)': ('Time In Seconds', 'max')}))
    evidence['Elapsed (min)'] = (evidence['End counter (sec)'] - evidence['Start counter (sec)']) / 60
    return evidence


def walkthrough_checks(cycle: pd.Series, records: pd.DataFrame, cycle_alerts: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({
        'Check': ['Reference threshold matched', 'Cycle is in ALERT status', 'Alert rows match alert count',
                  'No elapsed-counter reset', 'Raw record count matches cycle result'],
        'Result': [pd.notna(cycle['Reference Object']), cycle['Status'] == 'ALERT',
                   len(cycle_alerts) == cycle['Alert Count'],
                   not records['Time In Seconds'].diff().lt(0).any(),
                   len(records) == cycle['Record Count']],
    })


def parameter_traces(records: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Actual vs set point series per plot in PARAMETER_PLOTS (for trend lines)."""
    traces = {}
    for title, actual_col, set_point_col, activity, _ in PARAMETER_PLOTS:
        data = records if activity is None else records[records['Activity'].eq(activity)]
        traces[title] = data[['Date Time', actual_col, set_point_col]].rename(
            columns={actual_col: 'Actual', set_point_col: 'Set point'}).reset_index(drop=True)
    return traces


__all__ = ['ALERT_COLUMNS', 'DURATION_UNIT', 'build_alerts', 'cycle_overview', 'threshold_comparison',
           'alert_instant', 'tact_snapshot', 'tact_at_alerts', 'step_evidence', 'walkthrough_checks',
           'parameter_traces']

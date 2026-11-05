"""Validation checks for every pipeline stage, ported from the notebook.

Each function records its results in a ``ValidationLog``. ERROR checks raise
``ValidationError``; WARNING checks are recorded for the final report.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .config import ACTIVITIES, EXPECTED_RECIPES, EXPECTED_STEPS, VALID_RANGES, Settings
from .cycles import CYCLE_KEYS, cycle_minutes
from .rules import STATUS_ALERT, duration_limit, flow_limit, is_duration_breach
from .thresholds import DURATION_THRESHOLDS, FLOWS, PHASE_STEP_COLUMNS, THRESHOLD_COLUMNS, Reference
from .validation import ValidationLog, preview


def check_config(log: ValidationLog, settings: Settings, log_files: list[Path]) -> None:
    stage = 'Config'
    log.start_stage(stage)
    log.check(stage, 'Cycle gap is positive', settings.cycle_gap_minutes > 0,
              f'cycle_gap_minutes = {settings.cycle_gap_minutes}')
    for name, value in [('duration_tolerance', settings.duration_tolerance),
                        ('flow_tolerance', settings.flow_tolerance)]:
        log.check(stage, f'{name} is between 0 and 1', 0 <= value < 1, f'{name} = {value}')
    empty_files = [path.name for path in log_files if path.stat().st_size == 0]
    log.check(stage, 'No empty log files', not empty_files, preview(empty_files))


def check_load(log: ValidationLog, logs: pd.DataFrame, load_report: pd.DataFrame, settings: Settings) -> None:
    """File structure, missing or duplicate records, unknown names, sensor ranges, sampling rate, set points."""
    stage = 'Load'
    log.start_stage(stage)
    log.check(stage, 'Every log file has a line name in its header', load_report['Line From Header'].all(),
              preview(load_report.loc[~load_report['Line From Header'], 'Source File']))
    log.check(stage, 'Each line comes from only one file', not load_report['Line'].duplicated().any(),
              preview(load_report.loc[load_report['Line'].duplicated(keep=False), 'Source File']))
    log.check(stage, 'Only blank and unit rows lack a timestamp (at most 3 per file)',
              load_report['Rows Without Timestamp'].le(3).all(),
              preview(load_report.loc[load_report['Rows Without Timestamp'].gt(3), 'Source File']), level='WARNING')
    log.check(stage, 'No values lost converting measurements to numbers',
              load_report['Values Lost In Numeric Conversion'].eq(0).all(),
              f"{load_report['Values Lost In Numeric Conversion'].sum()} values became NaN", level='WARNING')

    for col in ['Object', 'Recipe', 'Activity', 'Step', 'Time In Seconds']:
        missing = logs[col].isna().sum()
        log.check(stage, f'{col} is filled on every record', missing == 0, f'{missing} records missing', level='WARNING')
    duplicates = logs.duplicated(['Line', 'Object', 'Date Time']).sum()
    log.check(stage, 'No duplicate records for the same object and timestamp', duplicates == 0,
              f'{duplicates} duplicates', level='WARNING')
    for col, expected in [('Recipe', EXPECTED_RECIPES), ('Activity', set(ACTIVITIES)), ('Step', EXPECTED_STEPS)]:
        unknown = set(logs[col].dropna()) - set(expected)
        log.check(stage, f'{col} values are all known', not unknown, preview(sorted(unknown)), level='WARNING')

    log.check(stage, 'Elapsed time is never negative', logs['Time In Seconds'].ge(0).all(),
              f"{logs['Time In Seconds'].lt(0).sum()} negative values", level='WARNING')
    for col, (low, high) in VALID_RANGES.items():
        outside = logs[col].notna() & ~logs[col].between(low, high)
        log.check(stage, f'{col} is within {low} to {high}', not outside.any(),
                  f'{outside.sum():,} records ({outside.mean():.1%}) outside; '
                  f'min {logs[col].min():.1f}, max {logs[col].max():.1f}', level='WARNING')

    interval = logs.groupby(['Line', 'Object'])['Date Time'].diff().dt.total_seconds().median()
    log.check(stage, f'Records are sampled about every {settings.sample_interval_seconds} s',
              abs(interval - settings.sample_interval_seconds) <= 5, f'median interval {interval:.0f} s',
              level='WARNING')

    # Odd set-point records usually carry the previous object's value at a switch-over.
    usual_set_point = logs.groupby('Object')['Min Return Flow SP'].transform(lambda s: s.mode().iloc[0])
    odd_set_point = logs['Min Return Flow SP'].ne(usual_set_point)
    log.check(stage, 'Min Return Flow SP is constant for each object', not odd_set_point.any(),
              f"{odd_set_point.sum()} records differ from their object's usual value", level='WARNING')

    periods = logs.groupby('Line')['Date Time'].agg(['min', 'max'])
    same_period = ((periods['min'].max() - periods['min'].min()) <= pd.Timedelta(days=1)
                   and (periods['max'].max() - periods['max'].min()) <= pd.Timedelta(days=1))
    log.check(stage, 'All lines cover the same period (within one day)', same_period,
              '; '.join(f'{line}: {row["min"]:%d %b} to {row["max"]:%d %b}' for line, row in periods.iterrows()),
              level='WARNING')


def check_thresholds(log: ValidationLog, reference: Reference, logs: pd.DataFrame) -> None:
    """Workbook structure, missing values, Summary vs Process totals, code coverage, PLC set point."""
    stage = 'Thresholds'
    log.start_stage(stage)
    summary, process, thresholds, lookup = reference.summary, reference.process, reference.thresholds, reference.lookup

    missing_columns = [col for cols in PHASE_STEP_COLUMNS.values() for col in cols if col not in process.columns]
    log.check(stage, 'Process sheet has every step column', not missing_columns, preview(missing_columns))
    log.check(stage, 'Summary has one row per object', not summary['Object'].duplicated().any(),
              preview(summary.loc[summary['Object'].duplicated(), 'Object']))
    not_in_process = sorted(set(summary['Object']) - set(process['Object']))
    log.check(stage, 'Every Summary object is in the Process sheet', not not_in_process, preview(not_in_process))

    missing_values = lookup[THRESHOLD_COLUMNS].isna().stack()
    missing_values = missing_values[missing_values]
    log.check(stage, 'No missing threshold values', missing_values.empty,
              preview(f'{obj}: {col}' for obj, col in missing_values.index), level='WARNING')
    not_positive = thresholds[THRESHOLD_COLUMNS].le(0).any(axis=1)
    log.check(stage, 'All thresholds are positive', not not_positive.any(),
              preview(thresholds.loc[not_positive, 'Object']))

    # Summary totals should equal the sum of the matching Process step times
    step_minutes = {phase: process.set_index('Object')[cols].sum(axis=1, min_count=len(cols))
                    .reindex(summary['Object']) / 60
                    for phase, cols in PHASE_STEP_COLUMNS.items()}
    summary_by_object = summary.set_index('Object')
    for label, summary_col, expected in [
        ('caustic time equals the Process caustic steps', 'Caustic- Total Time', step_minutes['Caustic']),
        ('current pre-rinse time equals the Process pre-rinse steps', 'Current-Total Pre rinse time(Min)',
         step_minutes['Pre Rinse']),
    ]:
        differs = ~np.isclose(summary_by_object[summary_col], expected, atol=0.05)
        log.check(stage, f'Summary {label}', not differs.any(), preview(summary_by_object.index[differs]),
                  level='WARNING')
    acid_with_mid_rinse = np.isclose(summary_by_object['Acid Total Time'],
                                     step_minutes['Acid'] + step_minutes['Mid Rinse'], atol=0.05)
    log.check(stage, 'Acid threshold covers only the Acid activity it is compared with',
              np.isclose(summary_by_object['Acid Total Time'], step_minutes['Acid'], atol=0.05).all(),
              f'{acid_with_mid_rinse.sum()} of {len(summary)} Acid Total Time values equal Mid Rinse + Acid Circulation',
              level='WARNING')

    duplicate_keys = thresholds.loc[thresholds['Object Key'].duplicated(keep=False), 'Object']
    log.check(stage, 'Each equipment code appears once in the workbook', duplicate_keys.empty,
              preview(duplicate_keys), level='WARNING')
    without_code = thresholds.loc[~thresholds['Object Key'].str.fullmatch(r'C\d{2}[A-Z]\d{2}'), 'Object']
    log.check(stage, 'Every workbook object has an equipment code', without_code.empty,
              preview(without_code), level='WARNING')
    unmatched_objects = sorted(logs.loc[logs['Reference Object'].isna(), 'Object'].unique())
    log.check(stage, 'Every log object has a reference threshold', not unmatched_objects,
              f"{logs['Reference Object'].notna().mean():.0%} of records matched; "
              f"unmatched: {preview(unmatched_objects, 10)}", level='WARNING')

    # The PLC logs its own minimum return flow set point, which should equal the workbook value
    plc_set_point = logs.dropna(subset=['Reference Object']).groupby('Reference Object')['Min Return Flow SP'].median()
    workbook_set_point = lookup['Return Flow Threshold (L/hr)'].reindex(plc_set_point.index)
    set_point_mismatch = plc_set_point.index[~np.isclose(plc_set_point, workbook_set_point)]
    log.check(stage, 'Workbook return flow equals the PLC Min Return Flow SP', set_point_mismatch.empty,
              preview(set_point_mismatch), level='WARNING')


def check_cycles(log: ValidationLog, logs: pd.DataFrame, cycle_results: pd.DataFrame,
                 merged_runs: pd.DataFrame, settings: Settings) -> None:
    """Every record in one cycle, no overlap, activity times add up, one complete run per cycle."""
    stage = 'Cycles'
    log.start_stage(stage)
    gap_minutes = settings.cycle_gap_minutes
    minutes = cycle_minutes(cycle_results)
    by_cycle = logs.groupby(CYCLE_KEYS, dropna=False)

    log.check(stage, 'Every record is assigned to a cycle', logs['Cycle Number'].notna().all())
    log.check(stage, 'One row per cycle', not cycle_results.duplicated(CYCLE_KEYS).any())
    log.check(stage, 'Cycle record counts add up to the log', cycle_results['Record Count'].sum() == len(logs),
              f"{cycle_results['Record Count'].sum():,} vs {len(logs):,} records")
    log.check(stage, 'Cycle start is never after cycle end',
              cycle_results['Cycle Start'].le(cycle_results['Cycle End']).all())

    ordered = cycle_results.sort_values(['Line', 'Object', 'Cycle Start'])
    gap_to_previous = (ordered['Cycle Start'] - ordered.groupby(['Line', 'Object'])['Cycle End'].shift()).dropna()
    log.check(stage, f'Cycles of the same object are more than {gap_minutes} min apart',
              gap_to_previous.gt(pd.Timedelta(minutes=gap_minutes)).all(),
              f'{gap_to_previous.le(pd.Timedelta(minutes=gap_minutes)).sum()} cycles overlap or are too close')

    # The counter restarts at every new CIP run, so a drop means two runs share one cycle
    log.check(stage, 'Each cycle holds a single CIP run (elapsed counter never restarts)', merged_runs.empty,
              f"{len(merged_runs)} cycles hold more than one run: {preview(merged_runs['Object'].unique())}",
              level='WARNING')
    repeated_activity = by_cycle['Activity'].agg(lambda s: s.ne(s.shift()).sum() > s.nunique())
    log.check(stage, 'Each activity occurs in one continuous block per cycle', not repeated_activity.any(),
              f'{repeated_activity.sum()} cycles repeat an activity', level='WARNING')

    activities = list(ACTIVITIES)
    log.check(stage, 'Activity times are never negative', cycle_results[activities].fillna(0).ge(0).all().all())
    over_cycle = cycle_results[activities].sum(axis=1) > minutes + 1
    log.check(stage, 'Activity times add up to no more than the cycle duration', not over_cycle.any(),
              f'{over_cycle.sum()} cycles', level='WARNING')
    sample_gaps = cycle_results['Record Count'] < 0.9 * (minutes * 60 / settings.sample_interval_seconds + 1)
    log.check(stage, 'No missing samples inside cycles', not sample_gaps.any(),
              f'{sample_gaps.sum()} cycles have under 90% of the expected records', level='WARNING')
    short_cycles = cycle_results['Record Count'] < settings.min_cycle_records
    log.check(stage, f'No cycle has fewer than {settings.min_cycle_records} records', not short_cycles.any(),
              f'{short_cycles.sum()} cycles', level='WARNING')
    long_cycles = minutes > settings.max_cycle_hours * 60
    log.check(stage, f'No cycle is longer than {settings.max_cycle_hours} h', not long_cycles.any(),
              f"{long_cycles.sum()} cycles: {preview(cycle_results.loc[long_cycles, 'Object'].unique())}",
              level='WARNING')

    chemical_without_rinse = cycle_results[['Caustic', 'Acid']].notna().any(axis=1) & cycle_results['Final Rinse'].isna()
    log.check(stage, 'Every cycle with caustic or acid has a final rinse', not chemical_without_rinse.any(),
              f"{chemical_without_rinse.sum()} cycles: "
              f"{preview(cycle_results.loc[chemical_without_rinse, 'Object'].unique())}", level='WARNING')
    no_flow = cycle_results['Supply Flow Actual (L/hr)'].le(0) | cycle_results['Return Flow Actual (L/hr)'].le(0)
    log.check(stage, 'Median cycle flows are positive', not no_flow.any(),
              f'{no_flow.sum()} cycles have a median flow at or below zero (pump-off readings are included)',
              level='WARNING')


def check_alert_logic(log: ValidationLog, cycle_results: pd.DataFrame, logs: pd.DataFrame,
                      reference: Reference, merged_runs: pd.DataFrame, settings: Settings) -> None:
    """Recalculate every breach flag and measure known threshold issues."""
    stage = 'Alert logic'
    log.start_stage(stage)
    tolerance = settings.duration_tolerance
    has_reference = cycle_results['Reference Object'].notna()
    log.check(stage, 'Cycles without a reference raise no alerts',
              cycle_results.loc[~has_reference, 'Alert Count'].eq(0).all())
    log.check(stage, 'Status is ALERT exactly when a matched cycle has a breach',
              cycle_results.loc[has_reference, 'Status'].eq(STATUS_ALERT)
              .eq(cycle_results.loc[has_reference, 'Alert Count'].gt(0)).all())
    for phase in DURATION_THRESHOLDS:
        recalculated = cycle_results[phase] > cycle_results[f'{phase} Threshold (min)'] * (1 + tolerance)
        log.check(stage, f'{phase} breach flags match a recalculation',
                  recalculated.eq(cycle_results[f'{phase} Breach']).all())
        no_threshold = has_reference & cycle_results[phase].notna() & cycle_results[f'{phase} Threshold (min)'].isna()
        log.check(stage, f'Every matched cycle with {phase} has a {phase} threshold', not no_threshold.any(),
                  f"{no_threshold.sum()} cycles: {preview(cycle_results.loc[no_threshold, 'Object'].unique())}",
                  level='WARNING')
    for flow in FLOWS:
        log.check(stage, f'{flow} flow remains monitoring-only', f'{flow} Flow Breach' not in cycle_results.columns)

    # Acid threshold = Mid Rinse + Acid, but the actual compared with it is Acid alone
    acid_with_mid_rinse = is_duration_breach(
        cycle_results[['Mid Rinse', 'Acid']].sum(axis=1).where(cycle_results['Acid'].notna()),
        cycle_results['Acid Threshold (min)'], tolerance)
    log.check(stage, 'Acid alerts are the same when Mid Rinse is included in the actual',
              acid_with_mid_rinse.sum() == cycle_results['Acid Breach'].sum(),
              f"{cycle_results['Acid Breach'].sum()} acid alerts now, {acid_with_mid_rinse.sum()} with Mid Rinse included",
              level='WARNING')

    # Pre-rinse threshold is the proposed timer, applied after the log period
    timer_change = reference.proposed_timer_change
    current_pre_rinse = cycle_results['Reference Object'].map(
        reference.summary.set_index('Object')['Current-Total Pre rinse time(Min)'])
    alerts_vs_current = is_duration_breach(cycle_results['Pre Rinse'], current_pre_rinse, tolerance).sum()
    change_text = 'unknown' if pd.isna(timer_change) else f'{timer_change:%d %b %Y}'
    log.check(stage, 'Proposed pre-rinse timers were in use during the log period',
              pd.isna(timer_change) or logs['Date Time'].min() >= timer_change,
              f"logs end {logs['Date Time'].max():%d %b %Y}, proposed timers applied from {change_text}; "
              f"{cycle_results['Pre Rinse Breach'].sum()} pre-rinse alerts vs proposed timer, "
              f"{alerts_vs_current} vs timer in use", level='WARNING')

    alerts_from_merged = cycle_results.merge(merged_runs, on=CYCLE_KEYS)['Alert Count'].gt(0).sum()
    log.check(stage, 'No alerts come from cycles holding several CIP runs', alerts_from_merged == 0,
              f'{alerts_from_merged} alerting cycles hold more than one run', level='WARNING')


def check_alerts(log: ValidationLog, alerts: pd.DataFrame, cycle_results: pd.DataFrame, settings: Settings) -> None:
    stage = 'Alerts'
    log.start_stage(stage)
    log.check(stage, 'One alert row per breach flag', len(alerts) == cycle_results['Alert Count'].sum(),
              f"{len(alerts)} rows vs {cycle_results['Alert Count'].sum()} breach flags")
    log.check(stage, 'No duplicate alerts', not alerts.duplicated(CYCLE_KEYS + ['Alert Type']).any())
    beyond = np.where(alerts['Unit'].eq('minutes'),
                      alerts['Actual Value'] > duration_limit(alerts['Threshold'], settings.duration_tolerance),
                      alerts['Actual Value'] < flow_limit(alerts['Threshold'], settings.flow_tolerance))
    log.check(stage, 'Every alert value is beyond its threshold and tolerance', beyond.all(), f'{(~beyond).sum()} rows')
    alert_cycles = alerts[CYCLE_KEYS].drop_duplicates().merge(cycle_results[CYCLE_KEYS + ['Status']],
                                                              on=CYCLE_KEYS, how='left')
    log.check(stage, 'Every alert belongs to a cycle in ALERT status', alert_cycles['Status'].eq(STATUS_ALERT).all())
    log.check(stage, 'Every ALERT cycle has at least one alert row',
              len(alert_cycles) == cycle_results['Status'].eq(STATUS_ALERT).sum())


def check_summaries(log: ValidationLog, counts: pd.DataFrame, per_day: pd.DataFrame,
                    cycle_results: pd.DataFrame, alerts: pd.DataFrame) -> None:
    stage = 'Summaries'
    log.start_stage(stage)
    log.check(stage, 'Cycle counts add up to all cycles', counts['Cycles'].sum() == len(cycle_results))
    log.check(stage, 'Daily cycles add up to all cycles', per_day['Cycles'].sum() == len(cycle_results))
    log.check(stage, 'Daily alerts add up to all alerts', per_day['Alert Count'].sum() == len(alerts))
    log.check(stage, 'Daily cycles with alerts add up to ALERT cycles',
              per_day['Cycles with Alerts'].sum() == cycle_results['Status'].eq(STATUS_ALERT).sum())

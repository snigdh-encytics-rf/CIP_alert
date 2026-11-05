"""Batch pipeline: the whole notebook as one function.

load logs -> load thresholds and match objects -> split cycles -> compare with
thresholds -> alerts -> summaries, with validation after every step.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from . import alerts as alerts_mod
from . import checks
from .config import Settings
from .cycles import assign_cycles, build_cycle_results, cycle_records, find_merged_runs
from .loader import load_logs
from .proposed_rules import PROPOSED_ALERT_COLUMNS, RuleSet, evaluate_batch, load_rule_set
from .rules import apply_rules
from .summaries import alerts_per_day, cycle_counts
from .thresholds import Reference, load_reference, match_objects
from .validation import ValidationLog


@dataclass
class BatchResult:
    settings: Settings
    logs: pd.DataFrame
    load_report: pd.DataFrame
    reference: Reference
    cycle_results: pd.DataFrame
    merged_runs: pd.DataFrame
    alerts: pd.DataFrame
    cycle_counts: pd.DataFrame
    alerts_per_day: pd.DataFrame
    validation: ValidationLog
    # Proposed T/A/C rules met (pending Danone sign-off). Evaluated whenever the rules are loaded ('show' or
    # 'alert'), so 'show' gives the projection; never part of ``alerts`` (the notebook's Time alerts).
    proposed_alerts: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=PROPOSED_ALERT_COLUMNS))
    rule_set: RuleSet | None = None

    def walkthrough(self, log_object: str, cycle_number: int) -> dict:
        """Debug view of one cycle (notebook "Stakeholder walkthrough")."""
        selected = self.cycle_results['Object'].eq(log_object) & self.cycle_results['Cycle Number'].eq(cycle_number)
        if not selected.any():
            raise KeyError(f'No cycle {cycle_number} for {log_object}')
        cycle = self.cycle_results.loc[selected].iloc[0]
        records = cycle_records(self.logs, log_object, cycle_number)
        cycle_alerts = self.alerts[self.alerts['Object'].eq(log_object)
                                   & self.alerts['Cycle Number'].eq(cycle_number)].reset_index(drop=True)
        instants, tact = alerts_mod.tact_at_alerts(records, cycle_alerts, self.settings.duration_tolerance)
        return {
            'overview': alerts_mod.cycle_overview(cycle),
            'comparison': alerts_mod.threshold_comparison(cycle, self.settings.duration_tolerance,
                                                          self.settings.flow_tolerance),
            'alerts': cycle_alerts[['Alert Type', 'Actual Value', 'Threshold', 'Unit']],
            'alert_instants': instants,
            'tact_at_alert': tact,
            'step_evidence': alerts_mod.step_evidence(records),
            'checks': alerts_mod.walkthrough_checks(cycle, records, cycle_alerts),
            'traces': alerts_mod.parameter_traces(records),
            'records': records,
        }


def run_batch(settings: Settings | None = None, logs: pd.DataFrame | None = None,
              load_report: pd.DataFrame | None = None, reference: Reference | None = None) -> BatchResult:
    """Run every stage. Pass ``logs``/``reference`` to reuse already-loaded data."""
    settings = settings or Settings()
    validation = ValidationLog()

    log_files = settings.log_files
    if logs is None:
        if not log_files:
            raise FileNotFoundError(f'No CIP log files found in {settings.data_dir.resolve()}')
        checks.check_config(validation, settings, log_files)
        logs, load_report = load_logs(log_files)
    logs = logs.copy()
    if load_report is not None:
        checks.check_load(validation, logs, load_report, settings)

    reference = reference or load_reference(settings.reference_file, settings.object_aliases)
    match_objects(logs, reference)
    checks.check_thresholds(validation, reference, logs)

    assign_cycles(logs, settings.cycle_gap_minutes)
    cycle_results = build_cycle_results(logs)
    merged_runs = find_merged_runs(logs)
    checks.check_cycles(validation, logs, cycle_results, merged_runs, settings)

    apply_rules(cycle_results, reference.lookup, settings.duration_tolerance)
    checks.check_alert_logic(validation, cycle_results, logs, reference, merged_runs, settings)

    alerts = alerts_mod.build_alerts(cycle_results)
    checks.check_alerts(validation, alerts, cycle_results, settings)

    counts = cycle_counts(cycle_results)
    per_day = alerts_per_day(cycle_results)
    checks.check_summaries(validation, counts, per_day, cycle_results, alerts)

    rule_set = load_rule_set(settings.proposed_rules, settings.proposed_rules_file)
    proposed = evaluate_batch(logs, rule_set)

    return BatchResult(settings=settings, logs=logs, load_report=load_report, reference=reference,
                       cycle_results=cycle_results, merged_runs=merged_runs, alerts=alerts,
                       cycle_counts=counts, alerts_per_day=per_day, validation=validation,
                       proposed_alerts=proposed, rule_set=rule_set)

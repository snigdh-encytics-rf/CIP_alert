"""Run the whole notebook pipeline on the log files and save the results.

    python scripts/run_batch.py                 # print a summary
    python scripts/run_batch.py --out output    # also write CSVs
    python scripts/run_batch.py --walk "C02C01-PMT TANK-1" 14
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cip_alerting import Settings, run_batch  # noqa: E402
from cip_alerting.proposed_rules import SIGN_OFF  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--data-dir', type=Path, help='folder with CIP_Log*.xlsx and the reference workbook')
    parser.add_argument('--out', type=Path, help='folder to write CSV results to')
    parser.add_argument('--walk', nargs=2, metavar=('OBJECT', 'CYCLE'), help='print the debug view of one cycle')
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format='%(message)s')
    pd.set_option('display.width', 200)
    pd.set_option('display.max_columns', 20)

    settings = Settings(data_dir=args.data_dir) if args.data_dir else Settings()
    result = run_batch(settings)

    print(f'\n{len(result.logs):,} records, {len(result.cycle_results):,} cycles across '
          f'{result.cycle_results["Object"].nunique()} objects')
    print(result.cycle_results['Status'].value_counts().to_string())
    print(f'\n{len(result.alerts):,} breaches')
    print(result.alerts['Alert Type'].value_counts().to_string())
    rules = result.rule_set
    if rules is not None and rules.active:   # projection of the proposed T/A/C rules (docs/TACT_rules_proposal.md)
        counts = result.proposed_alerts['Rule'].value_counts()
        print(f'\nProposed T/A/C rules ({SIGN_OFF}; mode {rules.mode}): '
              f'{len(result.proposed_alerts)} alerts they would raise')
        for rule in rules.rules:
            print(f'  {rule.id:<16} {counts.get(rule.id, 0):>3}  {rule.text}')
    print('\nValidation')
    print(result.validation.summary_table().to_string())

    if args.walk:
        view = result.walkthrough(args.walk[0], int(args.walk[1]))
        for name in ['overview', 'comparison', 'alerts', 'alert_instants', 'tact_at_alert', 'step_evidence', 'checks']:
            print(f'\n--- {name}')
            print(view[name].round(2).to_string())

    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        result.cycle_results.to_csv(args.out / 'cycle_results.csv', index=False)
        result.alerts.to_csv(args.out / 'alerts.csv', index=False)
        result.proposed_alerts.to_csv(args.out / 'proposed_alerts.csv', index=False)
        result.cycle_counts.to_csv(args.out / 'cycle_counts.csv')
        result.alerts_per_day.to_csv(args.out / 'alerts_per_day.csv', index=False)
        result.validation.report().to_csv(args.out / 'validation_report.csv', index=False)
        print(f'\nCSV files written to {args.out.resolve()}')


if __name__ == '__main__':
    main()

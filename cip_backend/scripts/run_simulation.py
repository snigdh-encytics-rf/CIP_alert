"""Replay the log files as 60-second IoT pings through the live monitor.

    python scripts/run_simulation.py                       # as fast as possible, print alerts
    python scripts/run_simulation.py --speed 60            # one ping per second
    python scripts/run_simulation.py --until "2026-07-10 12:00" --overview
    python scripts/run_simulation.py --events all --max-pings 200
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cip_alerting import LiveMonitor, Settings, Simulator, load_readings, load_reference  # noqa: E402
from cip_alerting.models import json_safe  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--speed', type=float, help='1 = real time, 60 = one ping per second; default: no waiting')
    parser.add_argument('--until', help='stop at this timestamp')
    parser.add_argument('--max-pings', type=int)
    parser.add_argument('--events', choices=['alerts', 'cycles', 'all', 'none'], default='alerts')
    parser.add_argument('--overview', action='store_true', help='print the overview page data as JSON at the end')
    args = parser.parse_args()

    settings = Settings(data_dir=args.data_dir) if args.data_dir else Settings()
    monitor = LiveMonitor(load_reference(settings.reference_file, settings.object_aliases), settings)
    readings = load_readings(settings)
    print(f'{len(readings):,} readings loaded; replaying as {settings.sample_interval_seconds}-second pings')

    shown = {'alerts': {'alert_raised'}, 'cycles': {'alert_raised', 'cycle_started', 'cycle_closed'},
             'all': None, 'none': set()}[args.events]

    def on_event(event):
        if shown is not None and event.kind not in shown:
            return
        if event.alert:
            a = event.alert
            print(f'{event.at:%Y-%m-%d %H:%M}  ALERT   {a.object} cycle {a.cycle_number}: {a.explanation}')
        else:
            print(f'{event.at:%Y-%m-%d %H:%M}  {event.kind:<16} {event.object} '
                  f'cycle {event.cycle_number} {json.dumps(json_safe(event.detail))}')

    Simulator(monitor, readings, settings.sample_interval_seconds, args.speed).run(
        on_event=on_event, until=pd.Timestamp(args.until) if args.until else None, max_pings=args.max_pings)

    print(f'\nClock {monitor.clock}: {len(monitor.alerts)} alerts, {len(monitor.closed_cycles)} closed cycles, '
          f'{monitor.runs_in_progress()} runs in progress')
    if args.overview:
        print(json.dumps(json_safe(monitor.overview()), indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()

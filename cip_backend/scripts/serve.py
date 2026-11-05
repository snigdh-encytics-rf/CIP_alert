"""Start the CIP Monitor: replay the logs as live 60-second pings (or take readings from gateways) and serve the UI.

    python scripts/serve.py                     # http://127.0.0.1:8000
    python scripts/serve.py --speed 600         # 10 simulated minutes per second
    python scripts/serve.py --start "2026-07-10 06:00" --port 8080
    python scripts/serve.py --mode live         # no replay: gateways POST /api/readings

By default the first part of the logs is processed instantly (so the page opens
with history) and the last 2 days play at --speed.

Every option can also be set with an environment variable (used by Docker);
a command-line flag wins over the variable:
    CIP_DATA_DIR, CIP_HOST, CIP_PORT, CIP_SPEED, CIP_START, CIP_LOOP (1/true/yes),
    CIP_MODE (replay|live), CIP_DB_PATH (SQLite file; default <cip_backend>/state/cip_monitor.db)
    CIP_PROPOSED_RULES (off|show|alert; default show: proposed T/A/C rules in the debug panel only, no alerts)
    CIP_STALE_AFTER_SECONDS (live mode: no reading for longer than this = stale feed; default 180)
The ingest token is read only from the environment (never a flag, so it stays out of `ps`):
    CIP_INGEST_TOKEN    when set, POST /api/readings needs "Authorization: Bearer <token>"
Notifications (all off unless set; see cip_alerting/notify.py):
    CIP_TEAMS_WEBHOOK, CIP_PUBLIC_URL, CIP_SMTP_HOST, CIP_SMTP_PORT, CIP_SMTP_USER, CIP_SMTP_PASSWORD,
    CIP_SMTP_FROM, CIP_SMTP_TO, CIP_SMTP_STARTTLS, CIP_NOTIFY_LINES, CIP_NOTIFY_MIN_INTERVAL,
    CIP_NOTIFY_IN_REPLAY, CIP_NOTIFY_MAX_LAG_MINUTES
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import webbrowser
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cip_alerting import Settings  # noqa: E402
from cip_alerting.api import MODES, ReplayService, serve  # noqa: E402
from cip_alerting.notify import Notifier  # noqa: E402
from cip_alerting.proposed_rules import MODES as RULE_MODES  # noqa: E402
from cip_alerting.proposed_rules import SIGN_OFF  # noqa: E402
from cip_alerting.store import open_store  # noqa: E402


def _env(name: str, default=None):
    value = os.environ.get(name, '').strip()
    return value if value else default


def _env_flag(name: str) -> bool:
    return _env(name, '').lower() in ('1', 'true', 'yes', 'on')


def _stop(*_) -> None:
    raise KeyboardInterrupt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--data-dir', type=Path, default=_env('CIP_DATA_DIR'))
    parser.add_argument('--host', default=_env('CIP_HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(_env('CIP_PORT', 8000)))
    parser.add_argument('--speed', type=float, default=float(_env('CIP_SPEED', 60)),
                        help='x real time; 60 = one simulated minute per second')
    parser.add_argument('--start', default=_env('CIP_START'),
                        help='replay instantly up to this time, then play at --speed '
                             '(default: 2 days before the end of the logs)')
    parser.add_argument('--open', action='store_true', help='open the page in a browser')
    parser.add_argument('--loop', action='store_true', default=_env_flag('CIP_LOOP'),
                        help='restart the replay when the logs run out')
    parser.add_argument('--mode', choices=MODES, default=_env('CIP_MODE', 'replay').lower(),
                        help='replay = play the log files; live = no replay, gateways POST /api/readings')
    parser.add_argument('--proposed-rules', choices=RULE_MODES, default=_env('CIP_PROPOSED_RULES', 'show').lower(),
                        help='proposed T/A/C rules (pending Danone sign-off): off, show (debug panel only) or '
                             'alert (they also raise alerts; changes alert counts)')
    parser.add_argument('--stale-after', type=float, default=float(_env('CIP_STALE_AFTER_SECONDS', 180)),
                        help='live mode: seconds without a reading before the feed counts as stale (default 180)')
    parser.add_argument('--db', default=_env('CIP_DB_PATH'),
                        help='SQLite file for acknowledgements, audit log and run history '
                             '(default <cip_backend>/state/cip_monitor.db)')
    args = parser.parse_args()
    if args.stale_after <= 0:
        parser.error('CIP_STALE_AFTER_SECONDS must be more than 0')
    if args.mode not in MODES:
        parser.error(f'CIP_MODE must be one of {", ".join(MODES)}, not {args.mode!r}')
    if args.proposed_rules not in RULE_MODES:
        parser.error(f'CIP_PROPOSED_RULES must be one of {", ".join(RULE_MODES)}, not {args.proposed_rules!r}')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')

    settings = Settings(proposed_rules=args.proposed_rules,
                        **({'data_dir': Path(args.data_dir)} if args.data_dir else {}))
    store = open_store(args.db)
    notifier = Notifier.from_env(store=store)
    token = _env('CIP_INGEST_TOKEN')
    print(f'Store: {store.path}' + ('' if store.persistent else '  (IN MEMORY: nothing survives a restart)'))
    if args.mode == 'replay':
        print('Loading log files and reference workbook...')
    service = ReplayService(settings, speed=args.speed, loop=args.loop, mode=args.mode, store=store,
                            ingest_token=token, notifier=notifier, stale_after_seconds=args.stale_after)
    rules = service.monitor.rule_set
    print(f'Proposed T/A/C rules: {rules.mode}' + ('' if rules.mode == 'off' else
          f' ({len(rules.rules)} rules, {SIGN_OFF}; ' + ('they RAISE ALERTS)' if rules.alerting else 'panel only)')))
    print(notifier.describe() + ('' if not notifier.configured or notifier.active(args.mode) else
                                 ' — inactive in replay mode (set CIP_NOTIFY_IN_REPLAY=1 to notify)'))
    if args.mode == 'replay':
        service.start_at = pd.Timestamp(args.start) if args.start else service.data_end - pd.Timedelta(days=2)
    elif not token:
        logging.warning('!' * 78 + '\nLIVE MODE WITHOUT CIP_INGEST_TOKEN: anyone who can reach this port can push '
                        'readings.\nSet CIP_INGEST_TOKEN to require "Authorization: Bearer <token>".\n' + '!' * 78)
    service.start()

    server = serve(service, args.host, args.port)
    shown_host = '127.0.0.1' if args.host in ('0.0.0.0', '', '::') else args.host
    url = f'http://{shown_host}:{args.port}/'
    if args.mode == 'replay':
        print(f'Replaying {len(service.readings):,} readings; fast-forward to {service.start_at:%d %b %Y %H:%M}, '
              f'then {args.speed:g}x real time.')
    else:
        print(f'Live mode: waiting for readings on POST {url}api/readings'
              + (' (bearer token required)' if token else ''))
    print(f'CIP Monitor running at {url}  (listening on {args.host}:{args.port}; Ctrl+C to stop)')
    if args.open:
        webbrowser.open(url)
    signal.signal(signal.SIGTERM, _stop)  # `docker stop` shuts down cleanly, like Ctrl+C
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')
    finally:
        server.server_close()
        service.stop()
        store.close()


if __name__ == '__main__':
    main()

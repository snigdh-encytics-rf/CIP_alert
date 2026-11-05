"""Device simulator: read the CIP log files and POST them to a CIP Monitor running in live mode.

This is the real IoT path end to end: a gateway sends each object's 60-second reading to
``POST /api/readings``; the server raises alerts the minute a limit is crossed.

    # terminal 1: a server that takes readings instead of replaying the logs
    CIP_MODE=live CIP_INGEST_TOKEN=secret python scripts/serve.py --port 8080
    # terminal 2: push the logs at 600x real time (10 simulated minutes per second)
    CIP_INGEST_TOKEN=secret python scripts/push_readings.py --url http://127.0.0.1:8080 --speed 600
    # everything as fast as possible, 2000 readings per request
    python scripts/push_readings.py --url http://127.0.0.1:8080 --speed 0 --batch-size 2000

--speed 0 sends as fast as the server accepts. Otherwise each minute's readings are sent together and
the script waits (gap / speed) seconds between minutes. The token is read from $CIP_INGEST_TOKEN (or --token).
Uses only the standard library for HTTP (pandas/openpyxl are needed to read the .xlsx logs).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cip_alerting import Settings  # noqa: E402
from cip_alerting.ingest import MAX_READINGS_PER_REQUEST, reading_payload  # noqa: E402
from cip_alerting.simulator import load_readings, pings  # noqa: E402


class PushError(RuntimeError):
    pass


def post(url: str, readings: list[dict], token: str | None = None, timeout: float = 60, retries: int = 3) -> dict:
    """POST one batch; retries on connection errors and 5xx. A retried batch is safe: readings the server
    already accepted come back as rejected 'duplicate' and the rest are added."""
    body = json.dumps({'readings': readings}).encode('utf-8')
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    for attempt in range(retries + 1):
        request = urllib.request.Request(url.rstrip('/') + '/api/readings', data=body, method='POST', headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as err:
            detail = err.read().decode('utf-8', 'replace')[:2000]
            if err.code < 500 or attempt == retries:
                raise PushError(f'HTTP {err.code}: {detail}') from None
        except (urllib.error.URLError, ConnectionError, TimeoutError) as err:
            if attempt == retries:
                raise PushError(f'cannot reach {url}: {err}') from None
        time.sleep(min(2 ** attempt, 10))
    raise AssertionError('unreachable')


def batches(readings: list, batch_size: int, speed: float, interval_seconds: int = 60):
    """Yield (first time, payloads, seconds to wait before sending). At speed 0 the readings are sent in
    chunks of batch_size; otherwise each minute is sent on its own (split if larger than batch_size)."""
    if not speed:
        for start in range(0, len(readings), batch_size):
            chunk = readings[start:start + batch_size]
            yield chunk[0].timestamp, [reading_payload(r) for r in chunk], 0.0
        return
    previous = None
    for slot, ping in pings(readings, interval_seconds):
        wait = 0.0 if previous is None else (slot - previous).total_seconds() / speed
        previous = slot
        payloads = [reading_payload(r) for r in ping]
        for start in range(0, len(payloads), batch_size):
            yield slot, payloads[start:start + batch_size], wait if start == 0 else 0.0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--url', default=os.environ.get('CIP_URL', 'http://127.0.0.1:8000'),
                        help='server base URL (default $CIP_URL or http://127.0.0.1:8000)')
    parser.add_argument('--data-dir', type=Path, default=None, help='folder with CIP_Log*.xlsx (default as serve.py)')
    parser.add_argument('--speed', type=float, default=60, help='x real time; 0 = as fast as possible (default 60)')
    parser.add_argument('--batch-size', type=int, default=1000, help=f'readings per request (max {MAX_READINGS_PER_REQUEST})')
    parser.add_argument('--from', dest='start', default=None, help='skip readings before this time')
    parser.add_argument('--until', default=None, help='stop after this time')
    parser.add_argument('--token', default=os.environ.get('CIP_INGEST_TOKEN') or None,
                        help='bearer token (default $CIP_INGEST_TOKEN)')
    parser.add_argument('--quiet', action='store_true', help='print only the final totals')
    args = parser.parse_args(argv)
    if not 1 <= args.batch_size <= MAX_READINGS_PER_REQUEST:
        parser.error(f'--batch-size must be 1-{MAX_READINGS_PER_REQUEST}')
    if args.speed < 0:
        parser.error('--speed must be >= 0')

    settings = Settings(data_dir=args.data_dir) if args.data_dir else Settings()
    readings = load_readings(settings)
    if args.start:
        readings = [r for r in readings if r.timestamp >= pd.Timestamp(args.start)]
    if args.until:
        readings = [r for r in readings if r.timestamp <= pd.Timestamp(args.until)]
    print(f'Pushing {len(readings):,} readings to {args.url} '
          f'({"as fast as possible" if not args.speed else f"{args.speed:g}x real time"}, '
          f'up to {args.batch_size} per request)')

    totals = {'accepted': 0, 'rejected': 0, 'requests': 0, 'alerts': 0}
    started = time.perf_counter()
    try:
        for slot, payloads, wait in batches(readings, args.batch_size, args.speed, settings.sample_interval_seconds):
            if wait:
                time.sleep(min(wait, 60))
            result = post(args.url, payloads, args.token)
            totals['requests'] += 1
            totals['accepted'] += result['accepted']
            totals['rejected'] += result['rejected']
            totals['alerts'] += len(result['alertsRaised'])
            if not args.quiet:
                for alert in result['alertsRaised']:
                    print(f'  ALERT {alert["raisedAt"]}  {alert["line"]}  {alert["objectId"]}  {alert["phase"]}')
                if result['rejected']:
                    print(f'  {slot}: {result["rejected"]} rejected, e.g. {result["rejections"][0]["message"]}')
    except KeyboardInterrupt:
        print('\nStopped.')
    except PushError as err:
        print(f'Error: {err}', file=sys.stderr)
        return 1
    print(f'Done in {time.perf_counter() - started:.1f} s: {totals["requests"]} requests, '
          f'{totals["accepted"]:,} accepted, {totals["rejected"]:,} rejected, {totals["alerts"]} alerts raised.')
    return 0


if __name__ == '__main__':
    sys.exit(main())

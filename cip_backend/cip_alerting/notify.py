"""Alert notifications: Microsoft Teams (incoming webhook) and email (SMTP). Standard library only.

Everything is OFF unless configured (environment variables, read by ``Notifier.from_env``):

=========================  ==============================================================================
CIP_TEAMS_WEBHOOK          Teams incoming-webhook / Workflows URL. Posts a compact Adaptive Card.
CIP_PUBLIC_URL             Base URL of the CIP Monitor page; the card and email link to ``<url>/#alert=<id>``.
CIP_SMTP_HOST              SMTP server; email is on when HOST, FROM and TO are set.
CIP_SMTP_PORT              Default 587. Port 465 uses implicit TLS (SMTP over SSL).
CIP_SMTP_USER / PASSWORD   Login (optional).
CIP_SMTP_FROM / TO         Sender; recipients as a comma-separated list.
CIP_SMTP_STARTTLS          1 (default) / 0: upgrade the connection with STARTTLS before logging in.
CIP_NOTIFY_LINES           Optional comma list of CIP lines (e.g. M11W01,M11W02); other lines are not notified.
CIP_NOTIFY_MIN_INTERVAL    Seconds between notifications for one object (default 300); alerts in between are
                           counted as throttled, not sent.
CIP_NOTIFY_IN_REPLAY       1 = also notify in replay (demo) mode. Default 0: replays never notify.
CIP_NOTIFY_MAX_LAG_MINUTES Live mode: an alert raised more than this many plant minutes before the newest
                           reading (a gateway uploading a backlog) is not notified. Default 10.
=========================  ==============================================================================

Only alerts raised while the monitor is live are sent: never during the replay's fast-forward warm-up, never
for an alert the store has seen before (a looped replay or a restart raises it again), never for one that is
already acknowledged. Messages go through a bounded queue to one background worker, so the replay or ingest
thread never waits on the network. Each channel is retried with exponential backoff; the outcome is written to
the audit log as ``notification_sent`` or ``notification_failed`` (channel, attempts, error; never a secret).
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import smtplib
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Callable, Mapping
from urllib.parse import quote, urlsplit

import pandas as pd

logger = logging.getLogger(__name__)

SIGN_OFF = 'Proposed — pending Danone sign-off'
DEFAULT_MIN_INTERVAL = 300.0
DEFAULT_MAX_LAG_MINUTES = 10.0
QUEUE_SIZE = 500
ACTIVITY_LABELS = {'Pre Rinse': 'Pre-rinse', 'Caustic': 'Caustic wash', 'Mid Rinse': 'Mid rinse',
                   'Acid': 'Acid wash', 'Final Rinse': 'Final rinse', 'Final Drain': 'Final drain'}
UNITS = {'L/hr': 'L/h'}


class PermanentError(Exception):
    """A failure that a retry cannot fix (e.g. 400/404 from the webhook, SMTP login refused)."""


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------
def _num(value, unit: str) -> str:
    if value is None or value != value:
        return 'no data'
    unit = UNITS.get(unit, unit)
    decimals = 0 if unit == 'L/h' else 1
    return f'{value:,.{decimals}f} {unit}'


OBJECT_NAME = re.compile(r'^\s*([A-Za-z0-9_]+)\s*-\s*(.+?)\s*$')   # same as api.split_object


def _split_object(name: str) -> tuple[str, str]:
    match = OBJECT_NAME.match(name)
    return (match.group(1), match.group(2)) if match else (name, '')


@dataclass(frozen=True)
class Message:
    alert_id: str
    line: str
    object: str
    code: str
    name: str
    title: str
    actual: str
    limit: str
    raised_at: str          # plant time, 'DD Mon YYYY HH:MM'
    cycle: int
    proposed: bool
    url: str | None

    @classmethod
    def from_alert(cls, alert, public_url: str | None = None) -> 'Message':
        code, name = _split_object(alert.object)
        phase = ACTIVITY_LABELS.get(alert.phase, alert.phase)
        if alert.proposed and alert.measured:
            m = alert.measured
            title = f'{m["title"]} (proposed rule)'
            actual = f'{_num(m["value"], m["unit"])} at raise, below the limit for {alert.actual_minutes:.0f} min'
            limit = f'{_num(m["limit"], m["unit"])} ({m["limitText"]}) for {m["minutes"]:g} min or longer'
        else:
            title = f'{phase} ran over its time limit'
            actual = f'{alert.actual_minutes:.1f} min'
            limit = (f'{alert.limit_minutes:.1f} min (threshold {alert.threshold_minutes:.1f} min '
                     f'+ {alert.tolerance:.0%})')
        url = f'{public_url.rstrip("/")}/#alert={quote(alert.id, safe="")}' if public_url else None
        return cls(alert_id=alert.id, line=alert.line, object=alert.object, code=code, name=name, title=title,
                   actual=actual, limit=limit, raised_at=pd.Timestamp(alert.raised_at).strftime('%d %b %Y %H:%M'),
                   cycle=alert.cycle_number, proposed=bool(alert.proposed), url=url)

    @property
    def subject(self) -> str:
        where = f'{self.code} {self.name}'.strip()
        return f'[CIP Monitor] {"[Proposed rule] " if self.proposed else ""}{where}: {self.title}'

    @property
    def text(self) -> str:
        lines = [self.title, '', f'Object:  {self.object} (line {self.line}, cycle {self.cycle})',
                 f'Actual:  {self.actual}', f'Limit:   {self.limit}', f'Raised:  {self.raised_at} (plant time)']
        if self.proposed:
            lines += ['', f'{SIGN_OFF}. This rule was derived from the July 2026 log data and is not an approved '
                          f'Danone rule.']
        if self.url:
            lines += ['', f'Open in CIP Monitor: {self.url}']
        return '\n'.join(lines) + '\n'


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------
class Channel:
    name = 'channel'

    def send(self, message: Message) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def secrets(self) -> list[str]:
        return []


class TeamsChannel(Channel):
    """Posts an Adaptive Card to a Teams incoming webhook (or a Workflows 'post to channel' webhook)."""
    name = 'teams'

    def __init__(self, url: str, timeout: float = 10.0):
        parts = urlsplit(url)
        if parts.scheme not in ('https', 'http') or not parts.netloc:
            raise ValueError('CIP_TEAMS_WEBHOOK must be an http(s) URL')
        if parts.scheme == 'http' and parts.hostname not in ('127.0.0.1', 'localhost', '::1'):
            logger.warning('CIP_TEAMS_WEBHOOK uses plain http: the webhook token travels unencrypted')
        self.url, self.timeout = url, timeout

    def secrets(self) -> list[str]:
        return [self.url, urlsplit(self.url).path] if urlsplit(self.url).path not in ('', '/') else [self.url]

    @staticmethod
    def payload(message: Message) -> dict:
        body = [
            {'type': 'TextBlock', 'text': f'CIP alert · line {message.line}', 'size': 'Small', 'isSubtle': True,
             'spacing': 'None'},
            {'type': 'TextBlock', 'text': f'{message.code}{f" · {message.name}" if message.name else ""}',
             'weight': 'Bolder', 'size': 'Medium', 'wrap': True, 'spacing': 'Small'},
            {'type': 'TextBlock', 'text': message.title, 'wrap': True, 'color': 'Attention', 'spacing': 'Small'},
            {'type': 'FactSet', 'facts': [{'title': 'Actual', 'value': message.actual},
                                          {'title': 'Limit', 'value': message.limit},
                                          {'title': 'Raised', 'value': f'{message.raised_at} (plant time)'},
                                          {'title': 'Cycle', 'value': str(message.cycle)}]},
        ]
        if message.proposed:
            body.append({'type': 'TextBlock', 'text': f'{SIGN_OFF}: derived from the July 2026 data, not an '
                                                      f'approved Danone rule.', 'wrap': True, 'isSubtle': True,
                         'size': 'Small'})
        card = {'$schema': 'http://adaptivecards.io/schemas/adaptive-card.json', 'type': 'AdaptiveCard',
                'version': '1.4', 'body': body, 'msteams': {'width': 'Full'}}
        if message.url:
            card['actions'] = [{'type': 'Action.OpenUrl', 'title': 'Open in CIP Monitor', 'url': message.url}]
        return {'type': 'message', 'summary': message.subject,
                'attachments': [{'contentType': 'application/vnd.microsoft.card.adaptive', 'contentUrl': None,
                                 'content': card}]}

    def send(self, message: Message) -> None:
        data = json.dumps(self.payload(message), ensure_ascii=False).encode('utf-8')
        request = urllib.request.Request(self.url, data=data, method='POST',
                                         headers={'Content-Type': 'application/json; charset=utf-8'})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response.read()
        except urllib.error.HTTPError as err:   # message without the URL (it holds the webhook secret)
            text = f'webhook answered HTTP {err.code}'
            if 400 <= err.code < 500 and err.code not in (408, 429):
                raise PermanentError(text) from None
            raise ConnectionError(text) from None
        except urllib.error.URLError as err:
            raise ConnectionError(f'webhook not reachable ({err.reason})') from None


class EmailChannel(Channel):
    name = 'email'

    def __init__(self, host: str, port: int, sender: str, recipients: list[str], user: str | None = None,
                 password: str | None = None, starttls: bool = True, timeout: float = 15.0):
        if not recipients:
            raise ValueError('CIP_SMTP_TO needs at least one address')
        self.host, self.port, self.sender, self.recipients = host, port, sender, recipients
        self.user, self.password, self.starttls, self.timeout = user, password, starttls, timeout

    def secrets(self) -> list[str]:
        return [s for s in (self.password,) if s]

    def build(self, message: Message) -> EmailMessage:
        mail = EmailMessage()
        mail['Subject'] = message.subject
        mail['From'] = self.sender
        mail['To'] = ', '.join(self.recipients)
        mail.set_content(message.text)
        return mail

    def send(self, message: Message) -> None:
        mail = self.build(message)
        try:
            if self.port == 465:
                smtp = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout,
                                        context=ssl.create_default_context())
            else:
                smtp = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
            with smtp:
                if self.starttls and self.port != 465:
                    smtp.starttls(context=ssl.create_default_context())
                if self.user:
                    smtp.login(self.user, self.password or '')
                smtp.send_message(mail, from_addr=self.sender, to_addrs=self.recipients)
        except (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused,
                smtplib.SMTPNotSupportedError) as err:
            raise PermanentError(f'SMTP refused: {type(err).__name__}') from None
        except (smtplib.SMTPException, OSError) as err:
            raise ConnectionError(f'SMTP failed: {type(err).__name__}: {err}') from None


# ---------------------------------------------------------------------------
# Notifier
# ---------------------------------------------------------------------------
@dataclass
class _Job:
    alert: object
    message: Message
    plant_time: object = None
    attempts: dict = field(default_factory=dict)


def _flag(value: str | None, default: bool = False) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in ('1', 'true', 'yes', 'on')


class Notifier:
    """Queues alert notifications and sends them from one background thread."""

    def __init__(self, channels: list[Channel] | None = None, *, lines: list[str] | None = None,
                 min_interval: float = DEFAULT_MIN_INTERVAL, public_url: str | None = None, in_replay: bool = False,
                 max_lag_minutes: float = DEFAULT_MAX_LAG_MINUTES, store=None, retries: int = 3,
                 backoff_seconds: float = 2.0, queue_size: int = QUEUE_SIZE,
                 clock: Callable[[], float] = time.monotonic):
        self.channels = list(channels or [])
        self.lines = {line.strip() for line in lines if line.strip()} if lines else None
        self.min_interval = max(0.0, float(min_interval))
        self.public_url = public_url or None
        self.in_replay = in_replay
        self.max_lag = pd.Timedelta(minutes=max_lag_minutes) if max_lag_minutes else None
        self.store = store
        self.retries = max(0, int(retries))
        self.backoff_seconds = backoff_seconds
        self.clock = clock
        self.queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self.counts: Counter = Counter()                       # queued, throttled, warming_up, ...
        self.sent: Counter = Counter()                         # per channel
        self.failed: Counter = Counter()
        self.last_error: dict[str, str] = {}
        self._last_sent: dict[tuple[str, str], float] = {}     # (line, object) -> clock() when queued
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.config_errors: list[str] = []

    # ------------------------------------------------------------ configuration
    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, store=None, **kwargs) -> 'Notifier':
        env = os.environ if env is None else env
        get = lambda name: (env.get(name) or '').strip()   # noqa: E731
        channels, errors = [], []
        if get('CIP_TEAMS_WEBHOOK'):
            try:
                channels.append(TeamsChannel(get('CIP_TEAMS_WEBHOOK')))
            except ValueError as exc:
                errors.append(str(exc))
        if get('CIP_SMTP_HOST') or get('CIP_SMTP_TO'):
            missing = [n for n in ('CIP_SMTP_HOST', 'CIP_SMTP_FROM', 'CIP_SMTP_TO') if not get(n)]
            if missing:
                errors.append(f'email is off: {", ".join(missing)} not set')
            else:
                try:
                    channels.append(EmailChannel(
                        get('CIP_SMTP_HOST'), int(get('CIP_SMTP_PORT') or 587), get('CIP_SMTP_FROM'),
                        [a.strip() for a in get('CIP_SMTP_TO').split(',') if a.strip()],
                        user=get('CIP_SMTP_USER') or None, password=get('CIP_SMTP_PASSWORD') or None,
                        starttls=_flag(env.get('CIP_SMTP_STARTTLS'), default=True)))
                except ValueError as exc:
                    errors.append(f'email is off: {exc}')

        def number(name, default):
            try:
                return float(get(name)) if get(name) else default
            except ValueError:
                errors.append(f'{name} must be a number; using {default:g}')
                return default
        notifier = cls(channels, lines=[x for x in get('CIP_NOTIFY_LINES').split(',') if x.strip()] or None,
                       min_interval=number('CIP_NOTIFY_MIN_INTERVAL', DEFAULT_MIN_INTERVAL),
                       public_url=get('CIP_PUBLIC_URL') or None, in_replay=_flag(env.get('CIP_NOTIFY_IN_REPLAY')),
                       max_lag_minutes=number('CIP_NOTIFY_MAX_LAG_MINUTES', DEFAULT_MAX_LAG_MINUTES), store=store,
                       **kwargs)
        notifier.config_errors = errors
        for error in errors:
            logger.warning('Notifications: %s', error)
        return notifier

    @property
    def configured(self) -> bool:
        return bool(self.channels)

    def active(self, mode: str) -> bool:
        """Whether alerts can be notified in this service mode."""
        return self.configured and (mode == 'live' or self.in_replay)

    def describe(self) -> str:
        if not self.configured:
            return 'Notifications: off (no channel configured)'
        parts = [c.name for c in self.channels]
        extra = [f'lines {",".join(sorted(self.lines))}' if self.lines else 'all lines',
                 f'at most one per object every {self.min_interval:g} s']
        return f'Notifications: {" + ".join(parts)} ({"; ".join(extra)})'

    # ------------------------------------------------------------ lifecycle
    def start(self) -> 'Notifier':
        if self.configured and self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._worker, name='cip-notify', daemon=True)
            self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the worker after the messages already queued (waits at most ``timeout`` seconds)."""
        if self._thread is None:
            return
        try:
            self.queue.put(None, timeout=0.5)
        except queue.Full:
            pass
        self._thread.join(timeout)
        self._stop.set()
        self._thread.join(1.0)
        self._thread = None

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Block until every queued message is handled (tests and shutdown)."""
        end = time.time() + timeout
        while time.time() < end:
            if self.queue.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return False

    # ------------------------------------------------------------ submit (called under the service lock)
    def submit(self, alert, *, mode: str, live: bool, plant_clock=None) -> str:
        """Queue a notification for a newly raised alert. Returns 'queued' or the reason it was not sent.

        ``live`` is False while the replay fast-forwards (warm-up / catch-up); ``plant_clock`` is the monitor clock.
        """
        reason = self._reason(alert, mode, live, plant_clock)
        if reason is None:
            message = Message.from_alert(alert, self.public_url)
            try:
                self.queue.put_nowait(_Job(alert, message, plant_clock))
                reason = 'queued'
            except queue.Full:
                reason = 'dropped'
                logger.warning('Notification queue full: alert %s not notified', alert.id)
        with self._lock:
            self.counts[reason] += 1
        return reason

    def _reason(self, alert, mode, live, plant_clock) -> str | None:
        if not self.configured:
            return 'not_configured'
        if mode != 'live' and not self.in_replay:
            return 'replay_off'
        if not live:
            return 'warming_up'
        if alert.acknowledged_at is not None:
            return 'acknowledged'
        if self.lines is not None and alert.line not in self.lines:
            return 'line_filtered'
        if (self.max_lag is not None and plant_clock is not None and mode == 'live'
                and pd.Timestamp(plant_clock) - pd.Timestamp(alert.raised_at) > self.max_lag):
            return 'backlog'
        key = (alert.line, alert.object)
        now = self.clock()
        with self._lock:
            last = self._last_sent.get(key)
            if last is not None and now - last < self.min_interval:
                return 'throttled'
            self._last_sent[key] = now
        return None

    # ------------------------------------------------------------ worker
    def _worker(self) -> None:
        while True:
            job = self.queue.get()
            try:
                if job is None:
                    return
                for channel in self.channels:
                    self._deliver(channel, job)
            except Exception:  # never let one bad message stop the worker
                logger.exception('Notification worker error')
            finally:
                self.queue.task_done()

    def _clean(self, channel: Channel, text: str) -> str:
        for secret in channel.secrets():
            if secret:
                text = text.replace(secret, '***')
        return text[:300]

    def _deliver(self, channel: Channel, job: _Job) -> None:
        attempt, error = 0, None
        while True:
            attempt += 1
            try:
                channel.send(job.message)
                with self._lock:
                    self.sent[channel.name] += 1
                self._audit('notification_sent', channel, job, {'attempts': attempt})
                logger.info('Notified %s: %s', channel.name, job.message.subject)
                return
            except PermanentError as exc:
                error = self._clean(channel, str(exc))
                break
            except Exception as exc:  # network, timeouts, 5xx
                error = self._clean(channel, f'{exc}' or type(exc).__name__)
                if attempt > self.retries or self._stop.is_set():
                    break
                if self._stop.wait(self.backoff_seconds * 2 ** (attempt - 1)):
                    break
        with self._lock:
            self.failed[channel.name] += 1
            self.last_error[channel.name] = error
        logger.warning('Notification via %s failed after %d attempt(s): %s', channel.name, attempt, error)
        self._audit('notification_failed', channel, job, {'attempts': attempt, 'error': error})

    def _audit(self, event: str, channel: Channel, job: _Job, details: dict) -> None:
        if self.store is None:
            return
        try:
            self.store.log_event(event, actor='notifier', plant_time=job.plant_time, alert=job.alert,
                                 details={'channel': channel.name, 'proposed': job.message.proposed, **details})
        except Exception:
            logger.exception('Could not record %s in the audit log', event)

    # ------------------------------------------------------------ status (never echoes a secret)
    def status(self, mode: str | None = None) -> dict:
        with self._lock:
            channels = {}
            for name in ('teams', 'email'):
                channel = next((c for c in self.channels if c.name == name), None)
                info = {'configured': channel is not None, 'sent': self.sent[name], 'failed': self.failed[name]}
                if name == 'email' and channel is not None:
                    info['recipients'] = len(channel.recipients)
                if self.last_error.get(name):
                    info['lastError'] = self.last_error[name]
                channels[name] = info
            return {
                'enabled': self.configured, 'active': self.active(mode) if mode else self.configured,
                'channels': channels, 'lines': sorted(self.lines) if self.lines else None,
                'minIntervalSeconds': self.min_interval, 'inReplay': self.in_replay,
                'linkToAlert': self.public_url is not None,
                'queued': self.queue.qsize(), 'counts': dict(self.counts),
                'configErrors': list(self.config_errors),
            }

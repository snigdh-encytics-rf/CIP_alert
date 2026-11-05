"""Notifications: Teams webhook payload (against a local fake webhook), email (fake SMTP), throttle, line
routing, warm-up suppression, retries, audit entries, status without secrets, and the replay integration."""
from __future__ import annotations

import json
import smtplib
import sys
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cip_alerting import Alert, Settings  # noqa: E402
from cip_alerting.api import ReplayService  # noqa: E402
from cip_alerting.notify import Channel, EmailChannel, Message, Notifier, TeamsChannel  # noqa: E402
from cip_alerting.store import Store  # noqa: E402

SETTINGS = Settings()
needs_data = pytest.mark.skipif(not (SETTINGS.log_files and SETTINGS.reference_file.exists()),
                                reason='datasets/ not available')
SECRET_PATH = '/webhookb2/abc-SECRET-TOKEN/IncomingWebhook/xyz'


def _alert(obj='C02C01-PMT TANK-1', line='M11W01', cycle=3, raised='2026-07-24 09:50', proposed=False) -> Alert:
    alert = Alert(id=f'{line}:{obj}:{cycle}:Caustic', line=line, object=obj, reference_object='REF',
                  cycle_number=cycle, phase='Caustic', alert_type='Caustic duration', raised_at=pd.Timestamp(raised),
                  step_at_raise='Circulation', actual_minutes=23.8, threshold_minutes=20.0, limit_minutes=22.0,
                  tolerance=0.1, rule='Caustic duration > Caustic threshold x 1.1', tact_at_raise=[])
    if proposed:
        alert.id += ':P-T-CAUSTIC'
        alert.proposed, alert.parameter, alert.rule_id = True, 'Temperature', 'P-T-CAUSTIC'
        alert.actual_minutes = 7.0
        alert.measured = {'title': 'Caustic wash temperature low', 'value': 72.4, 'unit': '°C', 'limit': 75.0,
                          'limitText': 'set point − 5 °C', 'minutes': 5.0, 'setPoint': 80.0, 'ruleId': 'P-T-CAUSTIC'}
    return alert


# ----------------------------------------------------------------- fake Teams webhook (local stdlib server)
class FakeWebhook:
    """Records POSTed JSON; answers with the next status in ``statuses`` (then 200)."""

    def __init__(self, statuses=()):
        self.bodies, self.statuses = [], list(statuses)
        hook = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                hook.bodies.append({'path': self.path, 'type': self.headers['Content-Type'], 'json': json.loads(body)})
                status = hook.statuses.pop(0) if hook.statuses else 200
                self.send_response(status)
                self.send_header('Content-Length', '1')
                self.end_headers()
                self.wfile.write(b'1')

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}{SECRET_PATH}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def webhook():
    hook = FakeWebhook()
    yield hook
    hook.close()


def test_teams_card_payload(webhook, tmp_path):
    store = Store(tmp_path / 'cip.db')
    notifier = Notifier([TeamsChannel(webhook.url)], public_url='https://cip.example.com/', store=store,
                        min_interval=0).start()
    alert = _alert()
    store.record_alert(alert)
    assert notifier.submit(alert, mode='live', live=True) == 'queued'
    assert notifier.wait_idle()
    notifier.stop()
    (post,) = webhook.bodies
    assert post['path'] == SECRET_PATH and post['type'].startswith('application/json')
    payload = post['json']
    assert payload['type'] == 'message'
    card = payload['attachments'][0]
    assert card['contentType'] == 'application/vnd.microsoft.card.adaptive'
    body = card['content']['body']
    texts = [b.get('text') for b in body if b['type'] == 'TextBlock']
    assert 'C02C01 · PMT TANK-1' in texts and 'Caustic wash ran over its time limit' in texts
    facts = {f['title']: f['value'] for b in body if b['type'] == 'FactSet' for f in b['facts']}
    assert facts['Actual'] == '23.8 min' and facts['Limit'].startswith('22.0 min')
    assert facts['Raised'] == '24 Jul 2026 09:50 (plant time)'
    (action,) = card['content']['actions']
    assert action['url'] == 'https://cip.example.com/#alert=M11W01%3AC02C01-PMT%20TANK-1%3A3%3ACaustic'
    sent = [e for e in store.audit() if e['event'] == 'notification_sent']
    assert sent[0]['details'] == {'channel': 'teams', 'proposed': False, 'attempts': 1}
    assert sent[0]['alertId'] == alert.id and sent[0]['actor'] == 'notifier'
    assert notifier.status()['channels']['teams'] == {'configured': True, 'sent': 1, 'failed': 0}


def test_proposed_alert_is_labelled_pending_sign_off():
    message = Message.from_alert(_alert(proposed=True))
    assert message.proposed and message.subject.startswith('[CIP Monitor] [Proposed rule]')
    assert message.limit == '75.0 °C (set point − 5 °C) for 5 min or longer'
    card = TeamsChannel('https://example.com/hook').payload(message)['attachments'][0]['content']
    assert any('pending Danone sign-off' in b.get('text', '') for b in card['body'])
    assert 'actions' not in card                              # no CIP_PUBLIC_URL: no link
    assert 'pending Danone sign-off' in message.text


def test_retries_with_backoff_then_success(tmp_path):
    hook = FakeWebhook(statuses=[500, 503])
    try:
        store = Store(tmp_path / 'cip.db')
        notifier = Notifier([TeamsChannel(hook.url)], store=store, backoff_seconds=0.01, min_interval=0).start()
        notifier.submit(_alert(), mode='live', live=True)
        assert notifier.wait_idle()
        notifier.stop()
        assert len(hook.bodies) == 3
        (sent,) = [e for e in store.audit() if e['event'] == 'notification_sent']
        assert sent['details']['attempts'] == 3
    finally:
        hook.close()


def test_permanent_failure_is_audited_without_secrets(tmp_path, caplog):
    hook = FakeWebhook(statuses=[404])
    try:
        store = Store(tmp_path / 'cip.db')
        notifier = Notifier([TeamsChannel(hook.url)], store=store, backoff_seconds=0.01, min_interval=0).start()
        notifier.submit(_alert(), mode='live', live=True)
        assert notifier.wait_idle()
        notifier.stop()
        assert len(hook.bodies) == 1                          # a 404 is not retried
        (failed,) = [e for e in store.audit() if e['event'] == 'notification_failed']
        assert failed['details']['attempts'] == 1 and 'HTTP 404' in failed['details']['error']
        status = notifier.status()
        assert status['channels']['teams']['failed'] == 1
        everything = json.dumps(status) + json.dumps(store.audit()) + store.audit_csv() + caplog.text
        assert 'SECRET-TOKEN' not in everything
    finally:
        hook.close()


def test_unreachable_webhook_is_retried_then_recorded(tmp_path):
    store = Store(tmp_path / 'cip.db')
    dead = FakeWebhook()
    url = dead.url
    dead.close()                                             # nothing listens on that port any more
    notifier = Notifier([TeamsChannel(url)], store=store, retries=2, backoff_seconds=0.01, min_interval=0).start()
    notifier.submit(_alert(), mode='live', live=True)
    assert notifier.wait_idle()
    notifier.stop()
    (failed,) = [e for e in store.audit() if e['event'] == 'notification_failed']
    assert failed['details']['attempts'] == 3 and 'not reachable' in failed['details']['error']
    assert 'SECRET-TOKEN' not in failed['details']['error']


# ----------------------------------------------------------------- email (fake SMTP)
class FakeSMTP:
    instances: list = []

    def __init__(self, host, port, timeout=None, **kwargs):
        self.host, self.port, self.calls, self.sent = host, port, [], []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append('quit')

    def starttls(self, context=None):
        self.calls.append('starttls')

    def login(self, user, password):
        self.calls.append(('login', user, password))

    def send_message(self, mail, from_addr=None, to_addrs=None):
        self.sent.append((mail, from_addr, to_addrs))


def test_email_via_fake_smtp(monkeypatch, tmp_path):
    FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, 'SMTP', FakeSMTP)
    env = {'CIP_SMTP_HOST': 'smtp.example.com', 'CIP_SMTP_PORT': '587', 'CIP_SMTP_USER': 'cip-bot',
           'CIP_SMTP_PASSWORD': 'hunter2-SECRET', 'CIP_SMTP_FROM': 'cip@example.com',
           'CIP_SMTP_TO': 'qa@example.com, ops@example.com', 'CIP_PUBLIC_URL': 'http://cip.local:8000'}
    store = Store(tmp_path / 'cip.db')
    notifier = Notifier.from_env(env, store=store).start()
    assert [c.name for c in notifier.channels] == ['email']
    notifier.submit(_alert(), mode='live', live=True)
    assert notifier.wait_idle()
    notifier.stop()
    (smtp,) = FakeSMTP.instances
    assert (smtp.host, smtp.port) == ('smtp.example.com', 587)
    assert smtp.calls[:2] == ['starttls', ('login', 'cip-bot', 'hunter2-SECRET')]
    mail, sender, recipients = smtp.sent[0]
    assert sender == 'cip@example.com' and recipients == ['qa@example.com', 'ops@example.com']
    assert mail['Subject'] == '[CIP Monitor] C02C01 PMT TANK-1: Caustic wash ran over its time limit'
    text = mail.get_content()
    assert 'Actual:  23.8 min' in text and 'Raised:  24 Jul 2026 09:50 (plant time)' in text
    assert 'http://cip.local:8000/#alert=M11W01%3AC02C01-PMT%20TANK-1%3A3%3ACaustic' in text
    status = notifier.status('live')
    assert status['channels']['email'] == {'configured': True, 'sent': 1, 'failed': 0, 'recipients': 2}
    assert 'hunter2' not in json.dumps(status) and 'cip-bot' not in json.dumps(status)
    assert 'qa@example.com' not in json.dumps(status)


def test_email_starttls_off_and_auth_failure_not_retried(monkeypatch, tmp_path):
    class Refusing(FakeSMTP):
        def login(self, user, password):
            raise smtplib.SMTPAuthenticationError(535, b'bad credentials')
    FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, 'SMTP', Refusing)
    store = Store(tmp_path / 'cip.db')
    channel = EmailChannel('smtp.local', 25, 'cip@x', ['qa@x'], user='u', password='SECRETPW', starttls=False)
    notifier = Notifier([channel], store=store, backoff_seconds=0.01).start()
    notifier.submit(_alert(), mode='live', live=True)
    assert notifier.wait_idle()
    notifier.stop()
    assert len(FakeSMTP.instances) == 1 and 'starttls' not in FakeSMTP.instances[0].calls
    (failed,) = [e for e in store.audit() if e['event'] == 'notification_failed']
    assert failed['details'] == {'channel': 'email', 'proposed': False, 'attempts': 1,
                                 'error': 'SMTP refused: SMTPAuthenticationError'}


# ----------------------------------------------------------------- routing rules (no network)
class Collect(Channel):
    name = 'teams'

    def __init__(self, delay: float = 0.0):
        self.messages, self.delay = [], delay

    def send(self, message):
        time.sleep(self.delay)
        self.messages.append(message)


def test_everything_off_unless_configured():
    notifier = Notifier.from_env({})
    assert not notifier.configured and notifier.submit(_alert(), mode='live', live=True) == 'not_configured'
    status = notifier.status('live')
    assert status['enabled'] is False and status['channels']['teams']['configured'] is False
    assert status['channels']['email']['configured'] is False
    half = Notifier.from_env({'CIP_SMTP_HOST': 'smtp.x'})    # email needs host, from and to
    assert not half.configured and 'CIP_SMTP_FROM, CIP_SMTP_TO not set' in half.status()['configErrors'][0]


def test_throttle_per_object():
    now = [1000.0]
    notifier = Notifier([Collect()], min_interval=300, clock=lambda: now[0])
    assert notifier.submit(_alert(cycle=1), mode='live', live=True) == 'queued'
    now[0] += 120
    assert notifier.submit(_alert(cycle=2), mode='live', live=True) == 'throttled'      # same object, 2 min later
    assert notifier.submit(_alert(obj='C02C02-PMT TANK-2'), mode='live', live=True) == 'queued'
    now[0] += 181
    assert notifier.submit(_alert(cycle=3), mode='live', live=True) == 'queued'         # 5 min after the first
    assert notifier.status()['counts'] == {'queued': 3, 'throttled': 1}


def test_line_routing_and_warm_up_and_replay_suppression():
    notifier = Notifier([Collect()], lines=['M11W01'], min_interval=0)
    assert notifier.submit(_alert(line='M11W02'), mode='live', live=True) == 'line_filtered'
    assert notifier.submit(_alert(line='M11W01'), mode='live', live=True) == 'queued'
    assert notifier.submit(_alert(), mode='live', live=False) == 'warming_up'
    assert notifier.submit(_alert(), mode='replay', live=True) == 'replay_off'      # default: replays never notify
    acked = _alert()
    acked.acknowledged_at = pd.Timestamp('2026-07-24 10:00')
    assert notifier.submit(acked, mode='live', live=True) == 'acknowledged'
    # live mode: a gateway uploading a backlog raises alerts far behind the newest reading
    assert notifier.submit(_alert(cycle=9), mode='live', live=True,
                           plant_clock=pd.Timestamp('2026-07-24 10:30')) == 'backlog'
    assert Notifier([Collect()], in_replay=True).submit(_alert(), mode='replay', live=True) == 'queued'
    env = Notifier.from_env({'CIP_TEAMS_WEBHOOK': 'https://x.example/hook', 'CIP_NOTIFY_LINES': 'M11W01, M01W02',
                             'CIP_NOTIFY_MIN_INTERVAL': '60', 'CIP_NOTIFY_IN_REPLAY': '1'})
    status = env.status('replay')
    assert status['lines'] == ['M01W02', 'M11W01'] and status['minIntervalSeconds'] == 60
    assert status['inReplay'] and status['active'] and 'x.example' not in json.dumps(status)


def test_submit_never_blocks_on_the_network():
    slow = Collect(delay=1.0)
    notifier = Notifier([slow], min_interval=0).start()
    started = time.perf_counter()
    for cycle in range(5):
        notifier.submit(_alert(cycle=cycle), mode='live', live=True)
    assert time.perf_counter() - started < 0.2               # queued; the worker sends in the background
    notifier.stop(timeout=0.1)


# ----------------------------------------------------------------- replay integration
@needs_data
def test_replay_notifies_only_alerts_raised_while_live(tmp_path):
    channel = Collect()
    notifier = Notifier([channel], in_replay=True, min_interval=0)
    store = Store(tmp_path / 'cip.db')
    service = ReplayService(replace(SETTINGS, proposed_rules='show'), speed=0, store=store, notifier=notifier)
    start_at = pd.Timestamp('2026-07-24 06:00')
    service.start_at = start_at
    notifier.start()
    service._run()
    assert notifier.wait_idle(20)
    live_alerts = [a for a in service.monitor.alerts.values() if a.raised_at >= start_at]
    assert live_alerts and len(channel.messages) == len(live_alerts)
    assert {m.alert_id for m in channel.messages} == {a.id for a in live_alerts}
    assert notifier.counts['warming_up'] == 221 - len(live_alerts)
    # a looped replay raises the same alerts again: nobody is notified twice
    service._restart_replay()
    service._replay_once()
    assert notifier.wait_idle(20)
    assert len(channel.messages) == len(live_alerts)
    status = service.status()['notifications']
    assert status['channels']['teams']['sent'] == len(live_alerts) and status['active']
    sent = [e for e in store.audit(1000) if e['event'] == 'notification_sent']
    assert len(sent) == len(live_alerts)
    notifier.stop()


@needs_data
def test_replay_without_opt_in_never_notifies():
    channel = Collect()
    service = ReplayService(SETTINGS, speed=0, notifier=Notifier([channel], min_interval=0))
    service.start_at = pd.Timestamp('2026-07-24 06:00')
    service._run()
    assert channel.messages == [] and service.notifier.counts['replay_off'] == 221
    assert service.status()['notifications']['active'] is False

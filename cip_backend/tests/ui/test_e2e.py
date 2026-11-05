"""End-to-end tests of the CIP Monitor page (v2, simplified UI) in headless Chrome (driven by ``cdp.py``).

Each test talks to a real server started by ``scripts/serve.py`` on a free port:

* ``live``   --speed 600: the clock moves 10 simulated minutes per second (live-update tests).
* ``ended``  a very high speed, so the replay finishes within seconds and the clock is frozen at
             the end of the logs. Tests that need stable data (selection, panel, acknowledge) use it.
* ``disconnect`` test starts and kills its own server.

Run:  cd cip_backend && /opt/anaconda3/bin/python3 -m pytest tests/ui -q -p no:cacheprovider

"""
from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
from datetime import datetime, timedelta
import urllib.request
from pathlib import Path
from urllib.parse import quote

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import websocket  # noqa: F401  (needed by cdp.py)
    from cdp import CHROME_CANDIDATES, Browser
    HAVE_CDP = True
except ImportError:  # pragma: no cover
    HAVE_CDP, CHROME_CANDIDATES = False, []

from cip_alerting import Settings  # noqa: E402

SETTINGS = Settings()
HAVE_CHROME = any(p and os.path.exists(p) for p in CHROME_CANDIDATES)
HAVE_DATA = bool(SETTINGS.log_files) and SETTINGS.reference_file.exists()

pytestmark = [
    pytest.mark.skipif(not HAVE_CDP, reason='websocket-client (cdp.py) not installed'),
    pytest.mark.skipif(not HAVE_CHROME, reason='Google Chrome not found (set CHROME_PATH)'),
    pytest.mark.skipif(not HAVE_DATA, reason='datasets/ not available'),
]

RED = 'rgb(200, 40, 28)'          # --red
AMBER = 'rgb(163, 84, 8)'         # --amber
PERIODS = ['24h', '7d', 'all']


# ---------------------------------------------------------------- server helpers
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, *args: str, port: int | None = None, env: dict | None = None):
        self.args, self.port = list(args), port or _free_port()
        self.base = f'http://127.0.0.1:{self.port}'
        self.proc = None
        # Each server gets its own database (kept across stop/start of this Server), so one test's
        # acknowledgements never leak into another test's server.
        folder = tempfile.mkdtemp(prefix='cip-e2e-')
        atexit.register(shutil.rmtree, folder, True)
        self.db = os.path.join(folder, 'cip_monitor.db')
        self.env = {**os.environ, 'CIP_DB_PATH': self.db, **(env or {})}

    def start(self, wait_state: str | None = 'live', timeout: float = 90) -> 'Server':
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / 'scripts' / 'serve.py'), '--port', str(self.port), *self.args],
            cwd=ROOT, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        end = time.time() + timeout
        while time.time() < end:
            if self.proc.poll() is not None:
                raise RuntimeError(f'server exited with {self.proc.returncode}')
            try:
                state = self.get('/api/status')['state']
                if wait_state is None or state == wait_state or (wait_state == 'live' and state == 'finished'):
                    return self
            except OSError:
                pass
            time.sleep(0.3)
        self.stop()
        raise TimeoutError(f'server on {self.port} did not reach {wait_state}')

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait(5)

    def get(self, path: str):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return json.loads(r.read())

    def post(self, path: str, body: bytes = b'{}', content_type: str = 'application/json'):
        req = urllib.request.Request(self.base + path, data=body, method='POST',
                                     headers={'Content-Type': content_type})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())


@pytest.fixture(scope='module')
def live():
    server = Server('--speed', '600').start('live')
    yield server
    server.stop()


@pytest.fixture(scope='module')
def ended():
    server = Server('--speed', '10000000').start('finished')
    yield server
    server.stop()


@pytest.fixture
def browser():
    with Browser(1600, 900) as b:
        yield b


# ---------------------------------------------------------------- page helpers
def open_page(b: Browser, server: Server, hash_: str = ''):
    b.goto(server.base + '/' + hash_)
    if hash_:
        b.wait_for("state.overview", 30)
        return
    b.wait_for("state.overview && document.querySelector('#list .row, #list .empty')", 30)
    b.wait_for("!state.selectedId || (state.detail && state.detail.alert.id === state.selectedId "
               "&& document.querySelector('#detail h2'))", 15)


def set_filters(b: Browser, period: str | None = None, line: str | None = None):
    for el, value in (('line', line), ('period', period)):
        if value is not None:
            b.eval(f"(() => {{ const s = document.getElementById('{el}'); s.value = {json.dumps(value)};"
                   f" s.dispatchEvent(new Event('change')); }})()")
    want = json.dumps(period or b.eval('state.period'))
    b.wait_for(f"state.overview && state.overview.period === {want}", 10)
    wait_for_poll(b)


def wait_for_poll(b: Browser, polls: int = 1):
    """Wait until the page has rendered ``polls`` more times (one render per 2-second poll)."""
    b.eval("window.__renders = 0; if (!window.__wrapped) { window.__wrapped = true; const r = window.render;"
           " window.render = (...a) => { const out = r(...a); window.__renders++; return out; }; }")
    b.wait_for(f'window.__renders >= {polls}', 5 * polls + 5)
    time.sleep(0.1)


def acknowledge(b: Browser):
    b.click('#ackBtn')
    b.wait_for("document.getElementById('ackBtn').disabled && "
               "document.getElementById('ackBtn').textContent.startsWith('Acknowledged')", 10)


def rows(b: Browser) -> list[dict]:
    return b.eval("[...document.querySelectorAll('#list .row')].map(r => ({id: r.dataset.id, key: r.dataset.key, "
                  "sel: r.classList.contains('sel'), ack: r.classList.contains('ack')}))")


NEWEST_OPEN = """(() => {
  const rows = state.overview.objects.filter(o => o.alerts.length).map(o => {
    const a = [...o.alerts].sort((x, y) => y.raisedAt.localeCompare(x.raisedAt));
    return a.find(x => !x.acknowledgedAt) || null; }).filter(Boolean);
  rows.sort((x, y) => y.raisedAt.localeCompare(x.raisedAt));
  return rows.length ? rows[0].id : null; })()"""


# ================================================================ load and selection
def test_initial_load_selects_newest_open_alert_and_highlights_its_row(browser, ended):
    open_page(browser, ended)
    assert browser.eval('state.selectedId') == browser.eval(NEWEST_OPEN)
    sel = [r for r in rows(browser) if r['sel']]
    assert len(sel) == 1 and sel[0]['id'] == browser.eval('state.selectedId')
    assert browser.eval("document.getElementById('pTitle').textContent").endswith('ran over its time limit')
    assert browser.errors == []


def test_click_and_keyboard_select_update_the_panel(browser, ended):
    open_page(browser, ended, )
    target = rows(browser)[2]
    browser.click(f'#list .row[data-id="{target["id"]}"]')
    browser.wait_for(f"state.detail && state.detail.alert.id === {json.dumps(target['id'])}", 10)
    assert [r for r in rows(browser) if r['sel']][0]['id'] == target['id']
    browser.eval("document.querySelector('#list .row').focus()")
    browser.key('ArrowDown', 'ArrowDown', 40)
    focused = browser.eval("document.activeElement.dataset.id")
    assert focused == rows(browser)[1]['id']
    browser.key('Enter', 'Enter', 13)
    browser.wait_for(f"state.detail && state.detail.alert.id === {json.dumps(focused)}", 10)
    wait_for_poll(browser)
    assert browser.eval("document.activeElement.dataset.id") == focused, 'focus survives the poll'


@pytest.mark.parametrize('period', PERIODS)
def test_summary_matches_the_data_for_every_line(browser, ended, period):
    open_page(browser, ended)
    for line in ['', *ended.get('/api/overview')['lines']]:
        set_filters(browser, period, line)
        got = browser.eval("[...document.querySelectorAll('#stats .v')].map(e => parseInt(e.textContent, 10))")
        ov = browser.eval('state.overview')
        alerts = [a for o in ov['objects'] for a in o['alerts']]
        unack = sum(not a['acknowledgedAt'] for a in alerts)
        want = [len(alerts), sum(bool(o['alerts']) for o in ov['objects']), sum(o['running'] for o in ov['objects'])]
        assert got == want, (period, line, got, want)
        assert len(rows(browser)) == want[1]
        open_badge = browser.eval("(document.querySelector('#list .count') || {}).textContent || ''")
        assert (open_badge == f'{unack} open') if unack else 'open' not in open_badge
        acked = browser.eval("(document.querySelector('#stats [data-filter=all] .v small') || {}).textContent || ''")
        assert acked == (f'{len(alerts) - unack} acknowledged' if len(alerts) > unack else '')


def test_quiet_objects_toggle(browser, ended):
    open_page(browser, ended)
    quiet = browser.eval("state.overview.objects.filter(o => !o.alerts.length).length")
    assert quiet > 0
    assert browser.eval("document.querySelector('.quiet').textContent").strip().startswith(f'✓\n') or \
        f'{quiet} object' in browser.eval("document.querySelector('.quiet').textContent")
    browser.click('.quiet')
    browser.wait_for(f"document.querySelectorAll('.qitem').length === {quiet}", 5)
    browser.click('.quiet')
    browser.wait_for("document.querySelectorAll('.qitem').length === 0", 5)


def test_panel_shows_the_essentials(browser, ended):
    open_page(browser, ended)
    d = browser.eval('state.detail')
    a = d['alert']
    text = browser.eval("document.getElementById('detail').innerText")
    assert f"+{browser.eval('mins(state.detail.alert.actualMinutes - state.detail.alert.limitMinutes)')} min over" in text
    for key in ('Limit', 'Crossed at'):
        assert key in text
    for gone in ('Threshold', '+10%', 'Not used'):
        assert gone not in text
    assert browser.eval("document.querySelectorAll('.track i').length") == 2
    assert browser.eval("document.querySelectorAll('.steps > div').length") == 6
    used = sum(t['parameter'] != 'Time' and t['status'] != 'not_applicable' for t in a['tact'])
    assert browser.eval("document.querySelectorAll('.reads .gauge').length") == used
    assert browser.eval("document.querySelectorAll('.chips .chip').length") >= 1
    for section in ('CIP STEPS', 'READINGS AT ALERT TIME', 'DATA CHECKS'):
        assert section in text.upper()
    assert not browser.eval("document.getElementById('pfoot').hidden")
    assert a['code'] in browser.eval("document.querySelector('.ptop').textContent")


def test_other_alert_selection_persists_and_back_to_latest(browser, ended):
    open_page(browser, ended, )
    browser.wait_for("document.querySelector('.other')", 10)
    other = browser.eval("document.querySelector('.other').dataset.id")
    browser.click('.other')
    browser.wait_for(f"state.detail && state.detail.alert.id === {json.dumps(other)}", 10)
    wait_for_poll(browser, 2)
    assert browser.eval('state.selectedId') == other
    assert any(r['sel'] for r in rows(browser)), "the object's row stays highlighted"
    if browser.eval("!!document.getElementById('latestBtn')"):
        browser.click('#latestBtn')
        browser.wait_for(f"state.selectedId !== {json.dumps(other)} && state.detail && state.detail.alert.id === state.selectedId", 10)


# ================================================================ layout
@pytest.mark.parametrize('size', [(1280, 720), (1366, 768), (1600, 900), (1920, 1080)])
def test_no_overflow_or_clipped_text(browser, ended, size):
    browser.viewport(*size)
    open_page(browser, ended)
    assert browser.eval("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    clipped = browser.eval("""[...document.querySelectorAll('.gtop b, .gtop .chip, .mtop b, .kv b, .chips .chip, .panel h2, .pill, .stat .v, .btn, .row .t')]
        .filter(e => e.scrollWidth > e.clientWidth + 1).map(e => e.textContent)""")
    assert clipped == [], clipped
    bar = browser.eval("[document.getElementById('ackBtn').getBoundingClientRect().top, document.getElementById('runBtn').getBoundingClientRect().top]")
    assert abs(bar[0] - bar[1]) < 1, 'the two buttons line up'
    assert browser.eval("document.getElementById('ackBtn').getBoundingClientRect().bottom") <= size[1], 'actions visible'


def test_narrow_screen_stacks_the_panel(browser, ended):
    browser.viewport(1000, 800)
    open_page(browser, ended)
    assert browser.eval("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    list_bottom = browser.eval("document.getElementById('list').getBoundingClientRect().bottom")
    assert browser.eval("document.getElementById('panel').getBoundingClientRect().top") >= list_bottom


# ================================================================ run dialog
def test_run_dialog_opens_and_closes_three_ways(browser, ended):
    open_page(browser, ended)
    for close in ("document.getElementById('dClose').click()", 'esc', 'backdrop'):
        browser.click('#runBtn')
        browser.wait_for("document.getElementById('runDialog').open", 5)
        assert browser.eval("document.querySelectorAll('#dBody tbody tr').length") >= 1
        assert browser.eval("document.getElementById('dTitle').textContent").startswith(browser.eval('state.detail.alert.code'))
        wait_for_poll(browser)
        assert browser.eval("document.getElementById('runDialog').open"), 'a poll does not close it'
        if close == 'esc':
            browser.key('Escape', 'Escape', 27)
        elif close == 'backdrop':
            browser.send('Input.dispatchMouseEvent', type='mousePressed', x=8, y=300, button='left', clickCount=1)
            browser.send('Input.dispatchMouseEvent', type='mouseReleased', x=8, y=300, button='left', clickCount=1)
        else:
            browser.eval(close)
        browser.wait_for("!document.getElementById('runDialog').open", 5)
        assert browser.eval("document.activeElement.id") == 'runBtn'


# ================================================================ live updates
def test_live_updates_keep_the_selection(browser, live):
    open_page(browser, live)
    chosen = rows(browser)[-1]['id']
    browser.click(f'#list .row[data-id="{chosen}"]')
    browser.wait_for(f"state.detail && state.detail.alert.id === {json.dumps(chosen)}", 10)
    clock = browser.eval('state.overview.clock')
    browser.wait_for(f"state.overview.clock !== {json.dumps(clock)}", 20)
    wait_for_poll(browser, 2)
    assert browser.eval('state.selectedId') == chosen
    assert browser.errors == []


# ================================================================ views
def test_run_history_lists_runs_and_opens_one(browser, ended):
    open_page(browser, ended, '#runs')
    browser.wait_for("document.querySelectorAll('#runsTable tbody tr').length > 0", 15)
    assert browser.eval("!document.getElementById('view-runs').hidden && document.getElementById('view-overview').hidden")
    assert browser.eval("document.querySelector('nav a[aria-current=page]').dataset.view") == 'runs'
    n = browser.eval("document.querySelectorAll('#runsTable tbody tr').length")
    assert browser.eval("document.getElementById('runsMeta').textContent").startswith(f'{n} run')
    browser.click('#runsTable tbody tr')
    browser.wait_for("document.getElementById('runDialog').open", 5)
    browser.key('Escape', 'Escape', 27)
    browser.wait_for("!document.getElementById('runDialog').open", 5)


def test_audit_log_filters_and_csv(browser, ended):
    open_page(browser, ended, '#audit')
    browser.wait_for("document.querySelectorAll('#auditTable tbody tr').length > 0", 15)
    assert browser.eval("document.getElementById('period').hidden"), 'the audit log is not period-filtered'
    browser.eval("(() => { const s = document.getElementById('aEvent'); s.value = 'alert_raised'; s.dispatchEvent(new Event('change')); })()")
    browser.wait_for("document.getElementById('csv').getAttribute('href').includes('event=alert_raised')", 10)
    assert browser.eval("[...document.querySelectorAll('#auditTable tbody .tag')].every(t => t.textContent === 'Alert raised')")
    href = browser.eval("document.getElementById('csv').getAttribute('href')")
    assert 'event=alert_raised' in href
    with urllib.request.urlopen(ended.base + href, timeout=10) as r:
        assert r.status == 200 and r.headers['Content-Type'].startswith('text/csv')


# ================================================================ acknowledge
def test_acknowledge_one_click_updates_counts_audit_and_survives_reload(browser, ended):
    open_page(browser, ended)
    alert_id = browser.eval('state.selectedId')
    before = browser.eval("parseInt(document.querySelector('#list .count').textContent, 10)")
    acknowledge(browser)
    browser.wait_for(f"parseInt(document.querySelector('#list .count').textContent, 10) === {before - 1}", 10)
    assert browser.eval("document.querySelector('.state').textContent").startswith('Acknowledged')
    browser.goto(ended.base + '/')
    browser.wait_for(f"state.overview && state.overview.objects.flatMap(o => o.alerts).some(a => a.id === {json.dumps(alert_id)} && a.acknowledgedAt)", 15)
    assert browser.eval('state.selectedId') != alert_id, 'the page moves on to an open alert'
    audit = ended.get('/api/audit?event=acknowledged&limit=5')['entries']
    assert any(e['alertId'] == alert_id and e['actor'] == 'Operator' for e in audit)


def test_acknowledgement_is_kept_after_a_server_restart():
    server = Server('--speed', '10000000').start('finished')
    try:
        with Browser(1600, 900) as b:
            open_page(b, server)
            alert_id = b.eval('state.selectedId')
            acknowledge(b)
            server.stop()
            server.start('finished')                      # same database file
            b.wait_for(f"state.connected && state.overview.objects.flatMap(o => o.alerts).some(a => a.id === {json.dumps(alert_id)} "
                       f"&& a.acknowledgedAt)", 60)
    finally:
        server.stop()


# ================================================================ states
def test_loading_skeleton_before_first_data(browser, ended):
    browser.send('Page.addScriptToEvaluateOnNewDocument', source=
                 "const __f = window.fetch; window.fetch = (...a) => new Promise(r => setTimeout(() => r(__f(...a)), 1500));")
    browser.goto(ended.base + '/')
    time.sleep(0.4)
    assert browser.eval("document.querySelectorAll('#stats .skel').length") > 0
    assert browser.eval("document.getElementById('live').textContent") == 'Connecting…'
    browser.wait_for("document.querySelector('#list .row')", 20)


def test_replay_ended_state(browser, ended):
    open_page(browser, ended)
    assert browser.eval("document.getElementById('live').textContent").startswith('Replay ended')


def test_disconnected_then_recovers():
    server = Server('--speed', '10000000').start('finished')
    try:
        with Browser(1600, 900) as b:
            open_page(b, server)
            selected = b.eval('state.selectedId')
            server.stop()
            b.wait_for("document.getElementById('live').textContent === 'Disconnected'", 10)
            assert b.eval("getComputedStyle(document.getElementById('live')).color") == RED
            assert not b.eval("document.getElementById('banner').hidden")
            assert 'Lost connection' in b.eval("document.getElementById('banner').textContent")
            assert b.eval("document.querySelectorAll('#list .row').length") > 0, 'keep showing the last data'
            assert b.eval("document.getElementById('ackBtn').disabled")
            assert b.eval("document.getElementById('period').disabled && document.getElementById('line').disabled")
            server.start('finished')
            b.wait_for("document.getElementById('live').textContent.startsWith('Replay ended')", 30)
            b.wait_for('state.detail && state.detail.alert.id === state.selectedId', 10)
            assert b.eval('state.selectedId') == selected
            assert b.eval("document.getElementById('banner').hidden")
            assert not b.eval("document.getElementById('ackBtn').disabled")
    finally:
        server.stop()


def test_never_connected_explains_itself(browser):
    browser.goto(f'http://127.0.0.1:{_free_port()}/')  # nothing listens; load the page from a file-less origin
    # the page itself cannot load without a server, so check the message logic against a stopped server instead
    server = Server('--speed', '10000000').start('finished')
    try:
        browser.goto(server.base + '/')
        browser.wait_for("state.everConnected", 15)
        browser.eval("state.everConnected = false")
        server.stop()
        browser.wait_for("!document.getElementById('banner').hidden", 10)
        assert "Can't reach the CIP Monitor service" in browser.eval("document.getElementById('banner').textContent")
    finally:
        server.stop()


# ================================================================ live mode (gateways push readings)
def _push(server: Server, readings: list[dict]):
    return server.post('/api/readings', json.dumps({'readings': readings}).encode())[1]


def _readings(first_minute: int, count: int) -> list[dict]:
    start = datetime(2026, 7, 24, 9, 0, 30)
    return [{'Date Time': (start + timedelta(minutes=m)).isoformat(), 'Line': 'M11W01',
             'Object': 'C02C01-PMT TANK-1', 'Recipe': 'Caustic', 'Activity': 'Caustic', 'Step': 'Circulation',
             'Time In Seconds': m * 60, 'Return Temp': 74.5, 'Return Flow': 18200.0, 'Return Conducivity': 92.0}
            for m in range(first_minute, first_minute + count)]


STALE_FETCH = """(() => { const f = window.fetch; window.fetch = async (...a) => {
  const r = await f(...a); if (!String(a[0]).includes('/api/overview')) return r;
  const d = await r.json(); d.status.lastReadingAgeSeconds = 420; d.status.state = 'stale';
  return new Response(JSON.stringify(d), {status: 200, headers: {'Content-Type': 'application/json'}}); }; })();"""


def test_live_mode_indicator_and_stale_banner():
    server = Server('--mode', 'live').start('waiting')
    try:
        with Browser(1600, 900) as b:
            b.goto(server.base + '/')
            b.wait_for("document.getElementById('live').textContent === 'Waiting for readings…'", 15)
            assert _push(server, _readings(0, 40))['accepted'] == 40   # caustic runs 39 min: over its limit
            b.wait_for("document.getElementById('live').textContent.startsWith('Live · last reading')", 10)
            b.wait_for("document.querySelectorAll('#list .row').length === 1", 10)
            assert b.eval("document.getElementById('banner').hidden")
            assert b.errors == []
        with Browser(1600, 900) as b:
            b.send('Page.addScriptToEvaluateOnNewDocument', source=STALE_FETCH)
            b.goto(server.base + '/')
            b.wait_for("document.getElementById('live').classList.contains('warn')", 15)
            assert b.eval("document.getElementById('live').textContent") == 'No data for 7 min'
            assert b.eval("getComputedStyle(document.getElementById('live')).color") == AMBER
            assert 'No readings received recently' in b.eval("document.getElementById('banner').textContent")
            assert not b.eval("document.getElementById('period').disabled"), 'the server is reachable: filters work'
    finally:
        server.stop()


# ================================================================ API errors
def test_api_errors(ended):
    with pytest.raises(urllib.error.HTTPError) as err:
        ended.get('/api/alerts/' + quote('no such alert', safe=''))
    assert err.value.code == 404 and json.loads(err.value.read())['error'] == 'Alert not found'
    with pytest.raises(urllib.error.HTTPError) as err:
        ended.get('/api/nothing')
    assert err.value.code == 404
    with pytest.raises(urllib.error.HTTPError) as err:
        ended.post('/api/alerts/x/acknowledge', b'{bad')
    assert err.value.code == 400
    with pytest.raises(urllib.error.HTTPError) as err:
        ended.post('/api/alerts/x/acknowledge', b'{"by": "qa"}')
    assert err.value.code == 404
    with pytest.raises(urllib.error.HTTPError) as err:
        ended.get('/api/overview?period=bogus')
    assert err.value.code == 400
    assert ended.get('/healthz')['ok'] is True
    for method in ('PUT', 'DELETE', 'PATCH', 'OPTIONS'):
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(urllib.request.Request(ended.base + '/api/status', method=method), timeout=10)
        assert err.value.code == 405


def test_notification_link_opens_that_alert(browser, ended):
    ov = ended.get('/api/overview?period=all')
    target = sorted((a for o in ov['objects'] for a in o['alerts']), key=lambda a: a['raisedAt'])[0]['id']  # oldest
    browser.goto(ended.base + '/#alert=' + quote(target, safe=''))
    browser.wait_for(f"state.detail && state.detail.alert.id === {json.dumps(target)}", 15)
    wait_for_poll(browser, 2)
    assert browser.eval('state.selectedId') == target, 'stays on the linked alert even outside the period'
    assert browser.eval('location.hash') == ''


def test_earlier_alerts_expand_to_the_full_list_and_collapse(browser, ended):
    recent = {o['objectId'] for o in ended.get('/api/overview?period=24h')['objects'] if o['alerts']}
    ov = ended.get('/api/overview?period=all')
    many = max((o for o in ov['objects'] if o['objectId'] in recent), key=lambda o: len(o['alerts']))
    alert_id = sorted(many['alerts'], key=lambda a: a['raisedAt'])[-1]['id']
    full = ended.get('/api/alerts/' + quote(alert_id, safe='') + '?others=all')
    assert len(full['otherAlerts']) == full['otherAlertCount'] > 6
    assert len(ended.get('/api/alerts/' + quote(alert_id, safe=''))['otherAlerts']) == 6
    browser.goto(ended.base + '/#alert=' + quote(alert_id, safe=''))
    browser.wait_for(f"state.detail && state.detail.alert.id === {json.dumps(alert_id)} && document.getElementById('moreBtn')", 15)
    assert browser.eval("document.querySelectorAll('.other').length") == 4
    assert browser.eval("document.getElementById('moreBtn').textContent") == f"+{full['otherAlertCount'] - 4} earlier"
    browser.click('#moreBtn')
    browser.wait_for(f"document.querySelectorAll('.other').length === {full['otherAlertCount']}", 10)
    wait_for_poll(browser)
    assert browser.eval("document.querySelectorAll('.other').length") == full['otherAlertCount'], 'stays expanded across polls'
    browser.eval("document.querySelectorAll('.other')[document.querySelectorAll('.other').length - 1].scrollIntoView()")
    last = browser.eval("document.querySelectorAll('.other')[document.querySelectorAll('.other').length - 1].dataset.id")
    browser.click(f'.other[data-id="{last}"]')
    browser.wait_for(f"state.detail && state.detail.alert.id === {json.dumps(last)}", 10)
    assert browser.eval("!!document.querySelector('#list .row.sel')"), "the object's row stays highlighted"
    browser.click('#latestBtn')
    browser.wait_for(f"state.selectedId !== {json.dumps(last)} && state.detail && state.detail.alert.id === state.selectedId", 10)
    assert browser.eval("document.querySelectorAll('.other').length") <= 4, 'a new selection starts collapsed'


def test_summary_cards_filter_the_list(browser, ended):
    open_page(browser, ended)
    browser.eval("(() => { const s = document.getElementById('period'); s.value = 'all'; s.dispatchEvent(new Event('change')); })()")
    browser.wait_for("state.overview && state.overview.period === 'all'", 10)
    wait_for_poll(browser)
    ov = browser.eval('state.overview')
    with_alerts = [o for o in ov['objects'] if o['alerts']]
    running = [o for o in ov['objects'] if o['running']]
    selected = browser.eval('state.selectedId')

    def click(f):
        browser.click(f'#stats [data-filter="{f}"]')
        browser.wait_for(f"state.filter === {json.dumps(f)} || ({json.dumps(f)} !== 'all' && state.filter === 'all')", 5)

    assert browser.eval("document.querySelectorAll('#stats [data-filter]').length") == 3
    assert browser.eval("!document.querySelector('#stats .on')"), 'no ring on the default view'
    click('objects')
    assert browser.eval("document.querySelector('#stats [data-filter=objects]').classList.contains('on')")
    wait_for_poll(browser)
    assert browser.eval('state.filter') == 'objects', 'the filter survives polls'
    assert browser.eval('state.selectedId') == selected, 'filtering keeps the panel on its alert'
    assert len(rows(browser)) == len(with_alerts)
    assert browser.eval("document.querySelectorAll('.qitem').length") == len(ov['objects']) - len(with_alerts)

    click('running')
    shown = len(rows(browser)) + browser.eval("document.querySelectorAll('.qitem').length")
    assert shown == len(running) or (not running and 'No CIP runs' in browser.eval("document.getElementById('list').textContent"))

    click('running')                                  # clicking the active card again clears it
    assert browser.eval('state.filter') == 'all'
    click('objects')
    browser.click('#clearFilter')
    browser.wait_for("state.filter === 'all'", 5)
    assert len(rows(browser)) == len(with_alerts)
    # the cards are native buttons, so Tab, Enter and Space work and screen readers announce the pressed state
    assert browser.eval("[...document.querySelectorAll('#stats [data-filter]')].every(b => b.tagName === 'BUTTON' && b.hasAttribute('aria-pressed'))")

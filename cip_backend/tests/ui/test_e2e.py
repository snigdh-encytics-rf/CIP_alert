"""End-to-end tests of the CIP Monitor page in headless Chrome (driven by ``cdp.py``).

Each test talks to a real server started by ``scripts/serve.py`` on a free port:

* ``live``   --speed 600: the clock moves 10 simulated minutes per second (live-update tests).
* ``ended``  a very high speed, so the replay finishes within seconds and the clock is frozen at
             the end of the logs. Tests that need stable data (selection, panel, acknowledge) use it.
* ``disconnect`` test starts and kills its own server.

Run:  cd cip_backend && /opt/anaconda3/bin/python3 -m pytest tests/ui -q -p no:cacheprovider

Tests marked ``xfail`` (strict) document known defects; each reason says what is wrong.
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

RED = 'rgb(200, 40, 28)'          # --critical
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
def open_page(b: Browser, server: Server, period: str | None = None, line: str | None = None):
    b.goto(server.base + '/')
    b.wait_for("state.overview && document.querySelectorAll('#rows tr').length > 0 "
               "&& !document.querySelector('#rows .empty')", 30)
    if period or line is not None:
        set_filters(b, period, line)
    b.wait_for("!state.selectedId || (state.detail && state.detail.alert.id === state.selectedId "
               "&& document.querySelector('#detail h2'))", 15)


def set_filters(b: Browser, period: str | None = None, line: str | None = None):
    for el, value in (('line', line), ('period', period)):
        if value is not None:
            b.eval(f"(() => {{ const s = document.getElementById('{el}'); s.value = {json.dumps(value)};"
                   f" s.dispatchEvent(new Event('change')); }})()")
    want = json.dumps(period or '24h')
    b.wait_for(f"state.overview && state.overview.period === {want}", 10)
    wait_for_poll(b)  # one full render with the new filters


def panel_matches_selection(b: Browser) -> bool:
    return b.eval("!!state.detail && state.detail.alert.id === state.selectedId")


def wait_for_poll(b: Browser, polls: int = 1):
    """Wait until the page has rendered ``polls`` more times (one render per 2-second poll)."""
    b.eval("window.__renders = 0; if (!window.__wrapped) { window.__wrapped = true; const r = window.render;"
           " window.render = (...a) => { const out = r(...a); window.__renders++; return out; }; }")
    b.wait_for(f'window.__renders >= {polls}', 5 * polls + 5)
    time.sleep(0.1)


def acknowledge(b: Browser, name: str | None = 'Test Operator', note: str | None = None, wait: bool = True):
    """Acknowledge the open alert through the popover: name (when the browser has none saved) and an optional note."""
    b.click('#ackBtn')
    b.wait_for("!document.getElementById('ackPop').hidden", 5)
    if name is not None and b.eval("!document.getElementById('apNameField').hidden"):
        b.eval(f"document.getElementById('apName').value = {json.dumps(name)}")
    if note:
        b.eval(f"(() => {{ const n = document.getElementById('apNote'); n.value = {json.dumps(note)};"
               f" n.dispatchEvent(new Event('input')); }})()")
    b.eval("document.getElementById('ackForm').requestSubmit()")
    if wait:
        b.wait_for("document.getElementById('ackPop').hidden && document.getElementById('ackBtn').disabled "
                   "&& document.getElementById('ackBtn').textContent === 'Acknowledged'", 10)


DOM_SUMMARY = """(() => {
  // a selection without reference thresholds shows '–' (nothing evaluated) instead of 0
  const v = [...document.querySelectorAll('.stat .v')].map(e => e.textContent.trim().startsWith('–') ? 0 : parseInt(e.textContent, 10));
  const of = parseInt(document.querySelector('.stat .v small').textContent.replace(/\\D/g, ''), 10);
  const rows = [...document.querySelectorAll('#rows tr.objrow')];
  const ar = [...document.querySelectorAll('tr.alertrow')];
  const shown = ar.reduce((s, r) => s + 1 + (parseInt(((r.querySelector('.more') || {}).textContent || '').match(/\\d+/)?.[0], 10) || 0), 0);
  const all = state.overview.objects.flatMap(o => o.alerts);
  return {stats: v, of, rows: rows.length, alertRows: ar.length, alertsShown: shown,
          unack: all.filter(a => !a.acknowledgedAt).length,
          running: state.overview.objects.filter(o => o.running).length};
})()"""


# ================================================================ initial load
def test_initial_load_selects_newest_unacknowledged_alert(browser, live):
    open_page(browser, live)
    page = browser.eval("""(() => {
        const all = state.overview.objects.flatMap(o => o.alerts);
        const open = all.filter(a => !a.acknowledgedAt).sort((a, b) => b.raisedAt.localeCompare(a.raisedAt));
        return {selected: state.selectedId, newest: open.length ? open[0].id : null,
                h2: document.querySelector('#detail h2')?.textContent, live: document.getElementById('live').textContent,
                title: document.title};
    })()""")
    # the selection was made from an earlier poll, so compare against alerts known at that time
    assert page['selected'], 'an alert should be selected on load'
    assert page['h2'] and page['h2'].endswith('ran over its time limit')
    assert page['live'].startswith(('Live', 'Catching up', 'Replay ended'))
    assert 'CIP Monitor' in page['title']
    sel = live.get('/api/alerts/' + quote(page['selected'], safe=''))['alert']
    assert sel['acknowledgedAt'] is None
    assert not browser.errors or all('favicon' in e or '404' in e for e in browser.errors), browser.errors


def test_initial_selection_in_stable_data_is_newest_unacknowledged(browser, ended):
    open_page(browser, ended)
    ov = ended.get('/api/overview?period=24h')
    open_alerts = sorted((a for o in ov['objects'] for a in o['alerts'] if not a['acknowledgedAt']),
                         key=lambda a: a['raisedAt'], reverse=True)
    assert browser.eval('state.selectedId') == open_alerts[0]['id']
    assert browser.eval("document.querySelector('tr.sel')?.dataset.id") == open_alerts[0]['id']
    assert browser.eval("document.querySelector('tr.sel').getAttribute('aria-pressed')") == 'true'


def test_no_failed_requests_on_load(browser, ended):
    browser.send('Network.enable')
    open_page(browser, ended)
    time.sleep(1)
    assert browser.errors == []


# ================================================================ table and selection
def test_click_row_updates_whole_panel(browser, ended):
    open_page(browser, ended, period='all')
    rows = browser.eval("[...document.querySelectorAll('tr.alertrow')].map(r => [r.dataset.id, r.querySelector('.obj').textContent])")
    assert len(rows) >= 3
    for row_id, code in rows[1:3]:
        browser.click(f'tr.alertrow[data-id="{row_id}"]')
        browser.wait_for(f"state.detail && state.detail.alert.id === {json.dumps(row_id)}", 10)
        time.sleep(0.2)
        assert browser.eval("document.querySelector('tr.sel').dataset.id") == row_id
        assert browser.eval("document.querySelector('.d-obj').textContent").startswith(code + ' · Cycle')
        assert browser.eval("document.querySelectorAll('tr.sel').length") == 1


def test_keyboard_tab_enter_space_select_with_visible_focus(browser, ended):
    open_page(browser, ended, period='all')
    for _ in range(10):  # Tab from the top bar into the first row
        browser.key('Tab', 'Tab', 9)
        if browser.eval("document.activeElement.matches('tr.alertrow')"):
            break
    assert browser.eval("document.activeElement.matches('tr.alertrow')"), 'rows must be reachable with Tab'
    assert browser.eval("document.activeElement.matches(':focus-visible')")
    ring = browser.eval("getComputedStyle(document.activeElement.querySelector('td')).boxShadow")
    assert ring and ring != 'none' and 'rgb(0, 94, 184)' in ring, 'focused row needs a visible focus ring'

    browser.key('Tab', 'Tab', 9)
    second = browser.eval('document.activeElement.dataset.id')
    browser.key('Enter', 'Enter', 13)
    browser.wait_for(f"state.detail && state.detail.alert.id === {json.dumps(second)}", 10)
    assert browser.eval("document.querySelector('tr.sel').dataset.id") == second

    browser.key('Tab', 'Tab', 9)
    third = browser.eval('document.activeElement.dataset.id')
    scroll = browser.eval("document.querySelector('.left').scrollTop")
    browser.key(' ', 'Space', 32)
    browser.wait_for(f"state.detail && state.detail.alert.id === {json.dumps(third)}", 10)
    assert browser.eval("document.querySelector('.left').scrollTop") == scroll, 'Space must not scroll the table'
    wait_for_poll(browser)
    assert browser.eval('document.activeElement.dataset.id') == third, 'row focus must survive a poll'


def test_focus_on_panel_buttons_survives_poll(browser, ended):
    open_page(browser, ended)
    browser.eval("document.getElementById('runBtn').focus()")
    wait_for_poll(browser)
    assert browser.eval('document.activeElement.id') == 'runBtn'


# ================================================================ period and line selectors
@pytest.mark.parametrize('period', PERIODS)
def test_summary_matches_rows_for_every_period_and_line(browser, ended, period):
    open_page(browser, ended)
    lines = browser.eval('state.overview.lines')
    assert len(lines) == 4
    for line in [''] + lines:
        set_filters(browser, period, line)
        s = browser.eval(DOM_SUMMARY)
        open_alerts, unack, with_alerts, running = s['stats']
        assert open_alerts == s['alertsShown'], (period, line, s)
        unmonitored = browser.eval("document.getElementById('stats').classList.contains('unmonitored')")
        assert unmonitored == (line in ('M01W01', 'M01W02')), (period, line, 'only lines without thresholds get the muted card')
        assert unack == s['unack'], (period, line, s)
        assert with_alerts == s['alertRows'], (period, line, s)
        assert s['of'] == s['rows'], (period, line, s)
        assert running == s['running'], (period, line, s)
        api = ended.get(f'/api/overview?period={period}&line={line}')['summary']
        assert [api['openAlerts'], api['unacknowledgedAlerts'], api['objectsWithAlerts'], api['runsInProgress']] == s['stats']
        if line:
            assert browser.eval("[...document.querySelectorAll('#rows .obj')].length") == s['rows']
            assert all(o['line'] == line for o in browser.eval('state.overview.objects'))


def test_line_without_alerts_shows_empty_state(browser, ended):
    open_page(browser, ended)
    set_filters(browser, 'all', 'M01W01')
    assert browser.eval("document.querySelectorAll('tr.alertrow').length") == 0
    # no object on M01W01 has thresholds: the heading must not read as 'healthy'
    assert browser.eval("document.querySelector('#detail .ep-title')?.textContent") == 'Line M01W01 isn’t monitored'
    assert browser.eval("document.querySelector('#ackBtn')") is None
    # switching back restores a selection
    set_filters(browser, '24h', '')
    browser.wait_for("document.querySelector('tr.sel') && document.querySelector('#detail h2')", 10)


def test_empty_state_does_not_claim_compliance_without_thresholds(browser, ended):
    open_page(browser, ended)
    set_filters(browser, 'all', 'M01W01')
    rows = browser.eval("[...document.querySelectorAll('#rows tr.objrow')].map(r => r.textContent)")
    assert rows and all('No reference thresholds' in r for r in rows)
    assert browser.eval("document.querySelectorAll('#rows tr.objrow svg[stroke=\"#54AD18\"]').length") == 0, \
        'unmonitored objects must not get the green tick'
    panel = browser.eval("document.getElementById('detail').innerText")
    assert 'within its time limits' not in panel and 'no alerts can be raised' in panel


# ================================================================ debug panel
def test_debug_panel_shows_all_fields(browser, ended):
    open_page(browser, ended)
    d = browser.eval("""(() => {
        const q = s => document.querySelector('#detail ' + s);
        return {tag: q('.tag').textContent, obj: q('.d-obj').textContent, h2: q('h2').textContent,
                why: q('.why').textContent,
                steps: [...document.querySelectorAll('.step')].map(s => [s.className, s.querySelector('.l').textContent, s.title]),
                naNote: q('.na-note')?.textContent || '',
                tact: [...document.querySelectorAll('.tact .p')].map(p => ({name: p.querySelector('.name').textContent,
                       st: p.querySelector('.st')?.textContent || '', read: p.querySelector('.read').textContent,
                       lim: p.querySelector('.lim')?.textContent || '', spark: !!p.querySelector('svg, .nospark')})),
                checks: [...document.querySelectorAll('ol.checks li')].map(l => l.textContent),
                ack: q('#ackBtn')?.textContent, run: q('#runBtn')?.textContent,
                phase: state.detail.alert.phase};
    })()""")
    assert d['tag'] == 'Open'
    assert ' · Cycle ' in d['obj']
    assert 'ran for' in d['why'] and 'tolerance' in d['why'] and 'limit was crossed at' in d['why']
    assert [s[1] for s in d['steps']] == ['Pre-rinse', 'Caustic', 'Mid rinse', 'Acid', 'Final rinse', 'Drain']
    now = [s for s in d['steps'] if 'now' in s[0].split()]
    assert len(now) == 1 and d['h2'].startswith(now[0][2].split(' (')[0])
    # parameters not used in this activity are named in one note line under the grid instead of empty tiles
    names = [p['name'] for p in d['tact']]
    assert names[-1] == 'Time' and names == [n for n in ['Temperature', 'Action', 'Concentration', 'Time'] if n in names]
    missing = [n for n in ['Temperature', 'Action', 'Concentration'] if n not in names]
    assert all(n.lower() in d['naNote'].lower() for n in missing) and (not missing or 'not used in' in d['naNote'])
    assert all(p['read'] and p['lim'] and p['spark'] for p in d['tact'])
    time_cell = d['tact'][-1]
    assert time_cell['st'] == 'Out of limit' and time_cell['read'].endswith('min')
    assert not time_cell['read'].endswith('.0 min'), 'whole minutes are shown without a trailing .0'
    # T/A/C have no approved rule; with the default CIP_PROPOSED_RULES=show a tile may carry the proposed evaluation
    assert all(p['st'] in ('Not evaluated', 'Invalid reading', 'No data') or p['st'].startswith('Proposed: ')
               for p in d['tact'][:-1])
    assert d['checks'], 'data checks list must say something, even when empty'
    assert d['ack'] == 'Acknowledge' and d['run'] == 'Open run'


@pytest.mark.parametrize('size', [(1280, 720), (1366, 768), (1440, 900), (1600, 900), (1920, 1080)])
def test_debug_panel_has_no_horizontal_overflow(browser, ended, size):
    browser.viewport(*size)
    open_page(browser, ended)
    m = browser.eval("(() => { const d = document.getElementById('detail'); return [d.scrollWidth, d.clientWidth]; })()")
    assert m[0] <= m[1], f'panel content is {m[0]}px wide in a {m[1]}px panel'


@pytest.mark.parametrize('size', [(1280, 720), (1366, 768)])
def test_alert_titles_readable_at_laptop_widths(browser, ended, size):
    browser.viewport(*size)
    open_page(browser, ended, period='all')
    bad = browser.eval("""[...document.querySelectorAll('tr.alertrow .alert, tr.alertrow .alert-sub')]
        .filter(e => e.scrollWidth > e.clientWidth + 1 && !e.title && !e.closest('[title]')).map(e => e.textContent)""")
    assert bad == []


def test_no_page_level_horizontal_scroll(browser, ended):
    for size in [(1280, 720), (1366, 768), (1440, 900), (1600, 900), (1920, 1080)]:
        browser.viewport(*size)
        open_page(browser, ended)
        assert browser.eval('document.documentElement.scrollWidth') <= size[0]
        assert browser.eval("document.querySelector('.top').scrollWidth <= document.querySelector('.top').clientWidth")
        # action buttons are reachable (sticky at the bottom of the panel)
        r = browser.eval("(() => { const r = document.getElementById('ackBtn').getBoundingClientRect(); return [r.top, r.bottom, r.height]; })()")
        assert r[1] <= size[1] and r[2] >= 44


def test_earlier_chip_selection_persists_across_polls(browser, ended):
    open_page(browser, ended, period='all')
    row = browser.eval("[...document.querySelectorAll('tr.alertrow')].find(r => r.querySelector('.more'))?.dataset.id")
    assert row, 'need an object with several alerts'
    browser.click(f'tr.alertrow[data-id="{row}"] .more')  # selects the row and opens its other alerts
    browser.wait_for("document.getElementById('others')?.open && document.querySelector('.chip').offsetParent", 10)
    chip = browser.eval("document.querySelector('.chip').dataset.id")
    browser.click('.chip')
    browser.wait_for(f"state.detail && state.detail.alert.id === {json.dumps(chip)}", 10)
    wait_for_poll(browser, 2)
    assert browser.eval('state.selectedId') == chip
    assert panel_matches_selection(browser)


def test_selected_alert_always_has_a_highlighted_row(browser, ended):
    open_page(browser, ended, period='all')
    row = browser.eval("[...document.querySelectorAll('tr.alertrow')].find(r => r.querySelector('.more'))?.dataset.id")
    browser.click(f'tr.alertrow[data-id="{row}"] .more')
    browser.wait_for("document.getElementById('others')?.open && document.querySelector('.chip').offsetParent", 10)
    browser.click('#others li:nth-child(2) .chip')
    browser.wait_for('state.pinned && state.detail && state.detail.alert.id === state.selectedId', 10)
    time.sleep(0.3)
    assert browser.eval("document.querySelector('tr.sel')") is not None


def test_earlier_chips_are_older_than_the_open_alert(browser, ended):
    open_page(browser, ended, period='all')
    row = browser.eval("[...document.querySelectorAll('tr.alertrow')].find(r => r.querySelector('.more'))?.dataset.id")
    browser.click(f'tr.alertrow[data-id="{row}"] .more')
    browser.wait_for("document.getElementById('others')?.open && document.querySelector('.chip').offsetParent", 10)
    browser.click('#others li:nth-child(2) .chip')
    browser.wait_for('state.pinned && state.detail && state.detail.alert.id === state.selectedId', 10)
    time.sleep(0.3)
    raised = browser.eval('state.detail.alert.raisedAt')
    others = browser.eval('state.detail.otherAlerts.map(o => o.raisedAt)')
    label = browser.eval("document.querySelector('#othersSum')?.textContent || ''")
    assert label.strip(), 'the other-alerts list needs a visible label'
    # the list mixes newer and older alerts, so it must not be labelled 'Earlier'
    assert not label.strip().startswith('Earlier') or all(c < raised for c in others)
    newer = [c for c in others if c > raised]
    if newer:  # a pinned older alert offers a way back to the latest one, which is badged
        assert browser.eval("!!document.getElementById('latestBtn')")
        assert browser.eval("document.querySelectorAll('#others .chip .latest').length") == 1


def test_implausible_readings_are_marked_not_shown_as_values(browser, ended):
    open_page(browser, ended, period='all')
    cells = browser.eval("""[...document.querySelectorAll('tr.alertrow .val.suspect')].map(e => [e.textContent, e.title])""")
    assert cells, 'the replay contains negative flow readings'
    # one term everywhere: 'Invalid' in the table, 'Invalid reading' in the tooltip and the panel
    assert all(t.endswith('Invalid') and title.startswith('Invalid reading') and '−' in title for t, title in cells)
    assert not browser.eval("[...document.querySelectorAll('.val')].some(e => /^-/.test(e.textContent))")


def test_rows_are_grouped_and_summary_label_follows_period(browser, ended):
    open_page(browser, ended, period='7d')
    groups = browser.eval("[...document.querySelectorAll('#rows tr.grp th')].map(t => t.textContent)")
    assert groups[0].startswith('Needs attention')
    assert browser.eval("document.querySelector('.stat .k').textContent") == 'Alerts · last 7 days'
    hidden = browser.eval("document.querySelectorAll('#rows tr.objrow[hidden]').length")
    if hidden:  # unmonitored objects are folded behind a labelled toggle
        browser.click('#toggleUnmon')
        browser.wait_for("document.querySelectorAll('#rows tr.objrow[hidden]').length === 0", 5)


# ================================================================ run dialog
def test_run_dialog_opens_and_closes_three_ways(browser, ended):
    open_page(browser, ended)
    code = browser.eval('state.detail.alert.code')

    def open_dialog():
        browser.click('#runBtn')
        browser.wait_for("document.getElementById('run').open", 5)

    open_dialog()
    text = browser.eval("document.getElementById('runBody').innerText")
    assert code in text and 'Activity times' in text and 'Flow' in text and 'reference threshold' in text
    assert browser.eval('document.activeElement.id') == 'runTitle', 'the dialog opens on its heading, not on ×'
    assert browser.eval("document.querySelectorAll('#runBody tbody tr').length") >= 3
    assert browser.eval("document.querySelector('#runBody .res-breach')") is not None
    wait_for_poll(browser)
    assert browser.eval("document.getElementById('run').open"), 'poll must not close the dialog'

    browser.click('#closeRun')
    browser.wait_for("!document.getElementById('run').open", 5)

    open_dialog()
    browser.key('Escape', 'Escape', 27)
    browser.wait_for("!document.getElementById('run').open", 5)
    assert browser.eval('document.activeElement.id') == 'runBtn', 'closing the dialog returns focus to Open run'

    open_dialog()
    for kind in ('mousePressed', 'mouseReleased'):  # backdrop, top-left corner of the viewport
        browser.send('Input.dispatchMouseEvent', type=kind, x=5, y=browser.height - 5, button='left', clickCount=1)
    browser.wait_for("!document.getElementById('run').open", 5)

    open_dialog()  # a click inside the dialog keeps it open
    x, y = browser.eval("(() => { const r = document.getElementById('run').getBoundingClientRect(); return [r.x + 8, r.y + 8]; })()")
    for kind in ('mousePressed', 'mouseReleased'):
        browser.send('Input.dispatchMouseEvent', type=kind, x=x, y=y, button='left', clickCount=1)
    time.sleep(0.3)
    assert browser.eval("document.getElementById('run').open")


# ================================================================ live updates
def test_live_updates_keep_selection_scroll_and_focus(browser, live):
    browser.viewport(1440, 900)
    open_page(browser, live)
    if live.get('/api/status')['state'] != 'live':
        pytest.skip('replay already finished')
    rows = browser.eval("[...document.querySelectorAll('tr.alertrow')].map(r => r.dataset.id)")
    target = rows[min(2, len(rows) - 1)]
    browser.click(f'tr.alertrow[data-id="{target}"]')
    browser.wait_for(f"state.detail && state.detail.alert.id === {json.dumps(target)}", 10)
    browser.eval(f"document.querySelector('tr[data-id=\"' + CSS.escape({json.dumps(target)}) + '\"]').focus({{preventScroll: true}})")
    browser.eval("document.querySelector('.left').scrollTop = 120; document.querySelector('.detail').scrollTop = 60")
    left, panel = browser.eval("[document.querySelector('.left').scrollTop, document.querySelector('.detail').scrollTop]")
    clock0 = browser.eval('state.overview.status.clock')
    deadline = time.time() + 30
    while time.time() < deadline and browser.eval('state.overview.status.clock') == clock0:
        time.sleep(0.5)
    assert browser.eval('state.overview.status.clock') > clock0, 'clock must advance while live'
    wait_for_poll(browser, 2)
    assert browser.eval('state.selectedId') == target
    assert browser.eval("document.querySelector('tr.sel')?.dataset.id") == target
    assert panel_matches_selection(browser)
    assert browser.eval("[document.querySelector('.left').scrollTop, document.querySelector('.detail').scrollTop]") == [left, panel]
    assert browser.eval('document.activeElement.dataset.id') == target
    assert browser.eval("document.getElementById('live').textContent").startswith(('Live', 'Replay ended'))
    assert browser.errors == [] or all('404' in e for e in browser.errors)


# ================================================================ acknowledge
def test_acknowledge_updates_state_and_persists(browser, ended):
    open_page(browser, ended)
    alert_id = browser.eval('state.selectedId')
    unack0 = browser.eval("parseInt(document.querySelectorAll('.stat .v')[1].textContent, 10)")
    open0 = browser.eval("parseInt(document.querySelectorAll('.stat .v')[0].textContent, 10)")
    acknowledge(browser, 'Test Operator', 'Checked valve V-12')
    assert browser.eval("document.querySelector('.tag').textContent").startswith('Acknowledged ')
    assert browser.eval("parseInt(document.querySelectorAll('.stat .v')[1].textContent, 10)") == unack0 - 1
    assert browser.eval("parseInt(document.querySelectorAll('.stat .v')[0].textContent, 10)") == open0
    row = f"tr.alertrow[data-id=\"{alert_id}\"]"
    assert browser.eval(f"document.querySelector({json.dumps(row)}).querySelector('.dot').classList.contains('ack')")
    assert 'Acknowledged' in browser.eval(f"document.querySelector({json.dumps(row)}).querySelector('.alert-sub').textContent")
    assert browser.eval('state.selectedId') == alert_id, 'selection must stay on the acknowledged alert'
    wait_for_poll(browser)
    assert browser.eval("document.getElementById('ackBtn').disabled")

    detail = ended.get('/api/alerts/' + quote(alert_id, safe=''))['alert']
    assert detail['acknowledgedAt'] and detail['acknowledgedBy'] == 'Test Operator'
    assert detail['acknowledgementNote'] == 'Checked valve V-12'

    open_page(browser, ended)  # reload: still acknowledged
    assert browser.eval('state.selectedId') != alert_id, 'reload selects the newest *unacknowledged* alert'
    browser.click(row)
    browser.wait_for(f"state.detail && state.detail.alert.id === {json.dumps(alert_id)}", 10)
    assert browser.eval("document.getElementById('ackBtn').disabled")


# ================================================================ states
def test_loading_state(browser, ended):
    browser.send('Fetch.enable', patterns=[{'urlPattern': '*/api/*'}])  # hold every API call
    browser.send('Page.navigate', url=ended.base + '/')
    browser.wait_for("document.readyState === 'complete'")
    time.sleep(0.5)
    assert browser.eval("document.querySelectorAll('#rows tr.skel').length") >= 3
    assert browser.eval("document.getElementById('rows').getAttribute('aria-busy')") == 'true'
    assert browser.eval("document.getElementById('live').textContent") == 'Connecting…'
    assert browser.eval("document.querySelector('#detail .skel-panel') !== null")
    assert 'Loading…' in browser.eval("document.getElementById('detail').textContent")


def test_loading_state_summary_band_is_not_empty(browser, ended):
    browser.send('Fetch.enable', patterns=[{'urlPattern': '*/api/*'}])
    browser.send('Page.navigate', url=ended.base + '/')
    browser.wait_for("document.readyState === 'complete'")
    time.sleep(0.5)
    assert browser.eval("document.getElementById('stats').children.length") == 4
    assert browser.eval("document.getElementById('stats').getBoundingClientRect().height") >= 100


def test_replay_ended_state(browser, ended):
    open_page(browser, ended)
    text = browser.eval("document.getElementById('live').textContent")
    assert text.startswith('Replay ended · ')
    assert 'idle' in browser.eval("document.getElementById('live').className")


def test_disconnected_then_recovers():
    server = Server('--speed', '10000000').start('finished')
    try:
        with Browser(1600, 900) as b:
            open_page(b, server)
            selected = b.eval('state.selectedId')
            server.stop()
            b.wait_for("document.getElementById('live').textContent.startsWith('Disconnected')", 10)
            assert b.eval("getComputedStyle(document.getElementById('live')).color") == RED
            assert 'last update' in b.eval("document.getElementById('live').textContent")
            assert b.eval("document.querySelectorAll('tr.alertrow').length") > 0, 'keep showing the last data'
            b.wait_for("!document.getElementById('banner').hidden", 5)
            assert 'Connection to the monitor lost' in b.eval("document.getElementById('banner').textContent")
            assert b.eval("document.body.classList.contains('stale')")
            assert b.eval("document.getElementById('ackBtn').disabled"), 'acknowledge is paused while disconnected'
            assert b.eval("document.getElementById('period').disabled && document.getElementById('line').disabled"), \
                'filters cannot load while disconnected, so they are paused too'
            server.start('finished')
            b.wait_for("!document.getElementById('live').textContent.startsWith('Disconnected')", 30)
            b.wait_for("document.getElementById('live').textContent.startsWith('Replay ended')", 30)
            b.wait_for('state.detail && state.detail.alert.id === state.selectedId', 10)
            assert b.eval('state.selectedId') == selected
            assert b.eval("document.getElementById('banner').hidden")
            assert not b.eval("document.getElementById('ackBtn').disabled")
            assert not b.eval("document.getElementById('period').disabled")
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
    assert err.value.code == 400 and '24h' in json.loads(err.value.read())['error']
    assert ended.get('/api/overview?line=NOPE')['objects'] == []  # a line may not have reported yet
    assert ended.get('/healthz')['ok'] is True
    with urllib.request.urlopen(urllib.request.Request(ended.base + '/', method='HEAD'), timeout=10) as r:
        assert r.status == 200 and r.read() == b''
    for method in ('PUT', 'DELETE', 'PATCH', 'OPTIONS'):  # JSON 405, not Python's HTML 501 page
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(urllib.request.Request(ended.base + '/api/status', method=method), timeout=10)
        assert err.value.code == 405 and err.value.headers['Allow'] == 'GET, HEAD, POST'
        assert 'error' in json.loads(err.value.read())


def test_acknowledge_with_non_object_body_returns_400(ended):
    with pytest.raises(urllib.error.HTTPError) as err:
        ended.post('/api/alerts/x/acknowledge', b'[]')
    assert err.value.code == 400
    with pytest.raises(urllib.error.HTTPError) as err:
        ended.post('/api/alerts/x/acknowledge', b'{"by": 5}')
    assert err.value.code == 400
    with socket.create_connection(('127.0.0.1', ended.port), timeout=10) as sock:  # bad Content-Length
        sock.sendall(b'POST /api/alerts/x/acknowledge HTTP/1.1\r\nHost: x\r\nContent-Length: abc\r\n\r\n')
        assert sock.recv(64).startswith(b'HTTP/1.0 400')


# ================================================================ last: mutates the 'ended' server heavily
def test_autoselect_always_highlights_a_row(browser, ended):
    ov = ended.get('/api/overview?period=all')
    for row in ov['objects']:  # acknowledge each object's latest alert; older ones stay open
        if row['alerts'] and not row['alerts'][0]['acknowledgedAt']:
            ended.post('/api/alerts/' + quote(row['alerts'][0]['id'], safe='') + '/acknowledge', b'{"by": "qa"}')
    open_page(browser, ended, period='all')
    browser.eval("state.selectedId = null")
    wait_for_poll(browser)
    assert browser.eval('state.selectedId')
    assert browser.eval("document.querySelector('tr.sel')") is not None


# ================================================================ round-2 QA additions
def press_enter(b: Browser):
    """A real Enter key press (with text), so native buttons and <summary> activate."""
    b.send('Input.dispatchKeyEvent', type='keyDown', key='Enter', code='Enter', windowsVirtualKeyCode=13, text='\r')
    b.send('Input.dispatchKeyEvent', type='keyUp', key='Enter', code='Enter', windowsVirtualKeyCode=13)


def test_keyboard_acknowledge_keeps_focus_in_panel():
    server = Server('--speed', '10000000').start('finished')  # own server: acknowledging mutates state
    try:
        with Browser(1600, 900) as b:
            open_page(b, server)
            b.eval("document.getElementById('ackBtn').focus()")
            press_enter(b)                                   # opens the popover, focus in the name field
            b.wait_for("!document.getElementById('ackPop').hidden && document.activeElement.id === 'apName'", 5)
            b.send('Input.insertText', text='Kim')
            press_enter(b)                                   # Enter in the name field submits
            b.wait_for("document.getElementById('ackBtn').textContent === 'Acknowledged'", 10)
            wait_for_poll(b)
            assert b.eval('document.activeElement.id') == 'runBtn', 'focus must stay in the panel, on Open run'
    finally:
        server.stop()


def test_other_alerts_list_opens_with_keyboard(browser, ended):
    open_page(browser, ended, period='all')
    assert browser.eval("!!document.getElementById('othersSum')")
    browser.eval("document.getElementById('othersSum').focus()")
    press_enter(browser)
    browser.wait_for("document.getElementById('others').open", 5)
    wait_for_poll(browser)
    assert browser.eval("document.getElementById('others').open"), 'the list must stay open across polls'
    browser.key('Tab', 'Tab', 9)
    assert browser.eval("document.activeElement.matches('.chip')")
    wait_for_poll(browser)
    assert browser.eval("document.activeElement.matches('.chip')"), 'chip focus must survive a poll'


def test_chip_activation_by_keyboard_keeps_focus(browser, ended):
    open_page(browser, ended, period='all')
    browser.eval("document.getElementById('othersSum').focus()")
    press_enter(browser)
    browser.wait_for("document.getElementById('others').open", 5)
    browser.eval("document.querySelector('.chip').focus()")
    chip = browser.eval("document.querySelector('.chip').dataset.id")
    press_enter(browser)
    browser.wait_for(f"state.detail && state.detail.alert.id === {json.dumps(chip)}", 10)
    wait_for_poll(browser)
    assert browser.eval('document.activeElement.id') in ('latestBtn', 'dTitle'), 'focus moves to Back to latest or the title'


def test_step_bar_classifies_every_step(browser, ended):
    open_page(browser, ended, period='all')
    rows = browser.eval("[...document.querySelectorAll('tr.alertrow')].map(r => r.dataset.id)")[:6]
    for row in rows:
        browser.click(f'tr.alertrow[data-id="{row}"]')
        browser.wait_for(f"state.detail && state.detail.alert.id === {json.dumps(row)}", 10)
        time.sleep(0.2)
        c = browser.eval('state.detail.cycle')
        classes = browser.eval("[...document.querySelectorAll('.step')].map(s => s.className.replace('step', '').trim())")
        assert classes.count('now') == 1
        if not c['running']:  # a finished run has no 'not reached yet' steps: each step either ran or was skipped
            assert all(k in ('done', 'over', 'now', 'skip') for k in classes), (row, classes, c['activityOrder'])


def test_more_link_selects_row_and_reveals_other_alerts(browser, ended):
    open_page(browser, ended, period='all')
    row = browser.eval("[...document.querySelectorAll('tr.alertrow:not(.sel)')].find(r => r.querySelector('.more'))?.dataset.id")
    assert row
    browser.click(f'tr.alertrow[data-id="{row}"] .more')
    browser.wait_for(f"state.detail && state.detail.alert.id === {json.dumps(row)} && document.getElementById('others')?.open", 10)
    time.sleep(0.8)  # smooth scroll
    top, bottom = browser.eval("(() => { const r = document.getElementById('others').getBoundingClientRect(); return [r.top, r.bottom]; })()")
    btn_top = browser.eval("document.querySelector('.btn').getBoundingClientRect().top")
    assert top >= 0 and top < btn_top, 'the other-alerts list must be scrolled into view, above the action bar'


def test_other_alert_counts_agree_between_row_and_panel(browser, ended):
    open_page(browser, ended)  # 24 h: most objects have far more alerts in the whole replay
    rows = browser.eval("[...document.querySelectorAll('tr.alertrow')].filter(r => r.querySelector('.more')).map(r => r.dataset.id)")
    assert rows
    for row in rows:
        browser.click(f'tr.alertrow[data-id="{row}"]')
        browser.wait_for(f"state.detail && state.detail.alert.id === {json.dumps(row)}", 10)
        time.sleep(0.2)
        more = int(browser.eval("document.querySelector('tr.sel .more').textContent.match(/\\d+/)[0]"))
        summary = browser.eval("document.getElementById('othersSum').textContent")
        count = int(browser.eval("document.querySelector('#othersSum .cnt').textContent"))
        total = browser.eval('state.detail.otherAlertCount')
        # the panel states the period count (matching the row) and the all-data count side by side
        assert count == more and 'in the last 24 h' in summary and f'{total} in all data' in summary, (row, more, summary)


def test_empty_state_actions_switch_filters(browser, ended):
    open_page(browser, ended)
    set_filters(browser, '24h', 'M01W02')
    assert browser.eval("document.querySelectorAll('tr.alertrow').length") == 0
    browser.click('[data-action="all-lines"]')
    browser.wait_for("state.line === '' && document.getElementById('line').value === '' && document.querySelector('#detail h2')", 10)
    set_filters(browser, '24h', 'M01W01')
    # a longer period can never add alerts to a line without thresholds, so only 'Show all lines' is offered
    assert browser.eval("[...document.querySelectorAll('#detail [data-action]')].map(b => b.dataset.action)") == ['all-lines']


def test_narrow_screen_stacks_panel_and_keeps_actions_reachable(browser, ended):
    browser.viewport(1024, 768)
    open_page(browser, ended)
    assert browser.eval('document.documentElement.scrollWidth') <= 1024
    assert browser.eval("document.querySelector('.top').scrollWidth <= document.querySelector('.top').clientWidth")
    browser.eval("document.getElementById('ackBtn').scrollIntoView({block: 'center'})")
    r = browser.eval("(() => { const r = document.getElementById('ackBtn').getBoundingClientRect(); return [r.top, r.bottom, r.width]; })()")
    assert 0 <= r[0] and r[1] <= 768 and r[2] > 100


@pytest.mark.parametrize('size', [(2560, 1440), (1024, 768)])
def test_extreme_widths_have_no_panel_overflow(browser, ended, size):
    browser.viewport(*size)
    open_page(browser, ended)
    m = browser.eval("(() => { const d = document.getElementById('detail'); return [d.scrollWidth, d.clientWidth]; })()")
    assert m[0] <= m[1]


def test_reacknowledge_keeps_first_acknowledgement():
    server = Server('--speed', '10000000').start('finished')
    try:
        alert_id = next(a['id'] for o in server.get('/api/overview?period=all')['objects'] for a in o['alerts'])
        path = '/api/alerts/' + quote(alert_id, safe='') + '/acknowledge'
        _, first = server.post(path, b'{"by": "alice"}')
        _, second = server.post(path, b'{"by": "bob"}')
        assert second['acknowledgedBy'] == 'alice' and second['acknowledgedAt'] == first['acknowledgedAt']
        with pytest.raises(urllib.error.HTTPError) as err:  # 'by' is capped
            server.post(path, json.dumps({'by': 'x' * 65}).encode())
        assert err.value.code == 400
    finally:
        server.stop()


def test_loop_restart_shows_notice():
    server = Server('--speed', '10000000', '--loop').start(None)
    try:
        with Browser(1600, 900) as b:
            b.goto(server.base + '/')
            b.wait_for("state.overview && state.overview.status.replayCount != null", 60)
            b.wait_for("!document.getElementById('toast').hidden && "
                       "document.getElementById('toastText').textContent.startsWith('Replay restarted')", 90)
            assert 'Acknowledgements are kept' in b.eval("document.getElementById('toastText').textContent")
            b.click('#toastClose')
            b.wait_for("document.getElementById('toast').hidden", 5)
            assert b.errors == []
    finally:
        server.stop()


# ================================================================ round-3 QA additions
def test_selected_row_has_accent_bar_distinct_from_hover(browser, ended):
    open_page(browser, ended)
    bar = browser.eval("getComputedStyle(document.querySelector('tr.sel td')).boxShadow")
    assert 'rgb(0, 94, 184)' in bar and 'inset' in bar, 'the selected row needs a left accent bar, not only a fill'
    sel_bg = browser.eval("getComputedStyle(document.querySelector('tr.sel td')).backgroundColor")
    hover_bg = browser.eval("getComputedStyle(document.documentElement).getPropertyValue('--row-hover').trim()")
    assert sel_bg != hover_bg


def test_whole_minutes_shown_without_decimal_in_table_and_dialog(browser, ended):
    open_page(browser, ended, period='all')
    pills = browser.eval("[...document.querySelectorAll('.val.breach')].map(e => e.textContent)")
    assert pills and not any(p.endswith('.0 min') for p in pills), pills
    browser.click('#runBtn')
    browser.wait_for("document.getElementById('run').open", 5)
    ran = browser.eval("[...document.querySelectorAll('#runBody tbody tr td:nth-child(2)')].map(e => e.textContent)")
    assert not any(r.endswith('.0') for r in ran), ran
    title = browser.eval("document.getElementById('runTitle').textContent")
    assert '—' not in title and ' · Cycle ' in title, title  # 'C01W01 · Milk Past-1 · Cycle 36'
    meta = browser.eval("document.getElementById('runBody').textContent")
    assert 'Reference:' not in meta, 'the raw reference key belongs in a tooltip only'
    browser.key('Escape', 'Escape', 27)


def test_data_checks_merge_the_two_flow_range_checks(browser, ended):
    open_page(browser, ended, period='all')
    rows = browser.eval("[...document.querySelectorAll('tr.alertrow')].map(r => r.dataset.id)")
    for row in rows:
        browser.click(f'tr.alertrow[data-id="{row}"]')
        browser.wait_for(f"state.detail && state.detail.alert.id === {json.dumps(row)}", 10)
        items = browser.eval("[...document.querySelectorAll('.checks li')].map(li => li.textContent)")
        assert not any('Conducivity' in i for i in items), items
        assert not any(i.count('outside the plausible sensor range') > 1 for i in items)
        assert sum('Supply flow' in i or 'Return flow' in i or 'return flow' in i for i in items) <= 1, items


def test_quiet_rows_show_last_reading_time_inline(browser, ended):
    open_page(browser, ended)
    texts = browser.eval("[...document.querySelectorAll('tr.quiet:not([hidden]) .ok')].filter(e => !e.classList.contains('noref')).map(e => e.textContent)")
    assert texts
    assert all(any(ch.isdigit() for ch in t) for t in texts), 'each completed/running row shows when'


def test_line_selector_pill_fits_its_value(browser, ended):
    open_page(browser, ended)
    wide = browser.eval("document.getElementById('line').getBoundingClientRect().width")
    set_filters(browser, '24h', 'M11W02')
    narrow = browser.eval("document.getElementById('line').getBoundingClientRect().width")
    assert narrow < wide - 30, (wide, narrow)


def test_restart_toast_does_not_cover_table_rows(browser, ended):
    open_page(browser, ended)
    browser.eval("toast('Replay restarted (pass 2): the log data plays again from the start. Acknowledgements are kept and reappear with their alerts.')")
    t = browser.eval("(() => { const r = document.getElementById('toast').getBoundingClientRect(); return [r.top, r.bottom, r.left, r.right]; })()")
    head = browser.eval("document.querySelector('.tbl thead').getBoundingClientRect().top")
    panel = browser.eval("document.getElementById('detail').getBoundingClientRect().left")
    assert t[1] <= head and t[3] <= panel, 'toast sits above the table, inside the left column'


def test_pinned_alert_outside_period_still_highlights_its_object_row(browser, ended):
    open_page(browser, ended)  # 24 h
    row = browser.eval("[...document.querySelectorAll('tr.alertrow')].map(r => r.dataset.id)[0]")
    browser.click(f'tr.alertrow[data-id="{row}"]')
    browser.wait_for(f"state.detail && state.detail.alert.id === {json.dumps(row)}", 10)
    browser.click('#othersSum')
    old = browser.eval("(() => { const d = document.querySelector('.olist li.div'); return d && d.nextElementSibling.querySelector('.chip').dataset.id; })()")
    assert old, 'needs an object with an alert before the last 24 h'
    browser.click(f'.chip[data-id="{old}"]')
    browser.wait_for(f"state.detail && state.detail.alert.id === {json.dumps(old)}", 10)
    wait_for_poll(browser)
    assert browser.eval("document.querySelectorAll('tr.sel').length") == 1
    # the row says which cycle the panel shows, with a lighter accent (its cells are the latest alert's)
    assert browser.eval("document.querySelector('tr.sel').classList.contains('pinned')")
    d = browser.eval("(() => { const a = state.detail.alert, r = state.overview.objects.flatMap(o => o.alerts).find(x => x.id === document.querySelector('tr.sel').dataset.id);"
                     " return {cycle: a.cycleNumber, phase: a.phase, rowCycle: r.cycleNumber, rowPhase: r.phase,"
                     " badge: document.querySelector('tr.sel .viewing').textContent, title: document.querySelector('tr.sel .viewing').title}; })()")
    assert d['badge'].startswith('Viewing '), d
    # the badge names whatever tells the panel's alert apart from the row's (phase and/or cycle), never only the row title's cycle
    if d['cycle'] != d['rowCycle']:
        assert f"cycle {d['cycle']}" in d['badge'], d
    assert f"cycle {d['cycle']}" in d['title'], d


def test_row_click_while_disconnected_keeps_row_and_panel_in_step():
    server = Server('--speed', '10000000').start('finished')
    try:
        with Browser(1600, 900) as b:
            open_page(b, server)
            server.stop()
            b.wait_for("!document.getElementById('banner').hidden", 10)
            other = b.eval("document.querySelector('tr.alertrow:not(.sel)').dataset.id")
            b.click(f'tr.alertrow[data-id="{other}"]')
            time.sleep(2.5)
            row_obj = b.eval("document.querySelector('tr.sel').dataset.obj")
            panel_obj = b.eval("state.detail.alert.line + '|' + state.detail.alert.objectId")
            assert row_obj == panel_obj, (row_obj, panel_obj)
    finally:
        server.stop()


def test_never_connected_explains_itself(browser, ended):
    browser.send('Page.addScriptToEvaluateOnNewDocument', source="window.fetch = () => Promise.reject(new TypeError('Failed to fetch'));")
    browser.goto(ended.base + '/', settle=5)
    assert not browser.eval("document.getElementById('banner').hidden")


def test_step_bar_flags_every_breached_step_of_the_cycle(browser, ended):
    open_page(browser, ended, period='all')
    found = browser.eval("""(async () => {
      const ids = state.overview.objects.flatMap(o => o.alerts.map(a => a.id));
      for (const id of ids){
        const d = await (await fetch('/api/alerts/' + encodeURIComponent(id))).json();
        if (d.cycle && d.cycle.activities.filter(x => x.breach).length > 1) return id;
      }
      return null; })()""")
    assert found
    browser.eval(f"select({json.dumps(found)}, true)")
    browser.wait_for(f"state.detail && state.detail.alert.id === {json.dumps(found)}", 10)
    time.sleep(0.3)
    breached = browser.eval("state.detail.cycle.activities.filter(x => x.breach && x.activity !== state.detail.alert.phase).map(x => x.activity)")
    steps = browser.eval("Object.fromEntries([...document.querySelectorAll('.step')].map((s, i) => [ACTIVITIES[i], s.className + ' ' + s.title]))")
    for act in breached:
        assert 'over' in steps[act].lower() or 'breach' in steps[act], (act, steps[act])


def test_needs_attention_count_matches_unacknowledged_stat(browser, ended):
    for period in PERIODS:
        open_page(browser, ended, period=period)
        unack = browser.eval("parseInt(document.querySelectorAll('.stat .v')[1].textContent, 10)")
        head = browser.eval("document.querySelector('#rows tr.grp.attn .cnt')?.textContent || ''")
        assert head == f'{unack} unacknowledged', (period, head, unack)
        # rows whose alerts are all acknowledged sit below the open ones, never between them
        kinds = browser.eval("[...document.querySelectorAll('#rows tr.grp.acked, #rows tr.alertrow')]"
                             ".map(r => r.classList.contains('grp') ? '|' : r.querySelector('.dot.open') ? 'o' : 'a')")
        open_part, _, acked_part = ''.join(kinds).partition('|')
        assert set(open_part) <= {'o'} and set(acked_part) <= {'a'}, kinds


@pytest.mark.parametrize('size', [(1280, 720), (1366, 768), (1600, 900)])
def test_alert_rows_stay_two_lines_at_laptop_widths(browser, ended, size):
    browser.viewport(*size)
    open_page(browser, ended, period='all')
    heights = browser.eval("[...document.querySelectorAll('tr.alertrow')].map(r => Math.round(r.getBoundingClientRect().height))")
    assert heights and max(heights) - min(heights) <= 1, heights


@pytest.mark.parametrize('size', [(1366, 768), (1600, 900), (1920, 1080)])
def test_data_checks_heading_is_above_the_action_bar(browser, ended, size):
    browser.viewport(*size)
    open_page(browser, ended)
    top, bar = browser.eval("""(() => { const h = [...document.querySelectorAll('#detail h3')].find(h => h.textContent.startsWith('Data checks'));
        return [h.getBoundingClientRect().bottom, document.querySelector('#detail .btn').getBoundingClientRect().top]; })()""")
    assert top <= bar, f'data checks heading ({top}) is hidden behind the action bar ({bar})'


# ================================================================ persistence and live mode (gateways push readings)
def test_acknowledgement_is_kept_after_a_server_restart():
    server = Server('--speed', '10000000').start('finished')
    try:
        with Browser(1600, 900) as b:
            open_page(b, server)
            alert_id = b.eval('state.selectedId')
            acknowledge(b, 'Asha', 'Restart check')
            server.stop()
            server.start('finished')                      # same database file
            b.wait_for("!document.getElementById('toast').hidden && "
                       "document.getElementById('toastText').textContent.startsWith('The monitor restarted')", 60)
            assert 'Acknowledgements are kept' in b.eval("document.getElementById('toastText').textContent")
            b.wait_for(f"state.overview.objects.flatMap(o => o.alerts).some(a => a.id === {json.dumps(alert_id)} "
                       f"&& a.acknowledgedBy === 'Asha' && a.acknowledgementNote === 'Restart check')", 15)
            # only while it was down (a poll cut off by the stop reports a content-length mismatch)
            assert all('ERR_CONNECTION_REFUSED' in e or 'ERR_CONTENT_LENGTH_MISMATCH' in e for e in b.errors), b.errors
    finally:
        server.stop()


def _push(server: Server, readings: list[dict]):
    return server.post('/api/readings', json.dumps({'readings': readings}).encode())[1]


def _readings(first_minute: int, count: int) -> list[dict]:
    start = datetime(2026, 7, 24, 9, 0, 30)
    return [{'Date Time': (start + timedelta(minutes=m)).isoformat(), 'Line': 'M11W01',
             'Object': 'C02C01-PMT TANK-1', 'Recipe': 'Caustic', 'Activity': 'Caustic', 'Step': 'Circulation',
             'Time In Seconds': m * 60, 'Return Temp': 74.5, 'Return Flow': 18200.0, 'Return Conducivity': 92.0}
            for m in range(first_minute, first_minute + count)]


# Makes the page believe the last reading arrived 7 minutes ago (the server's own stale state is tested in
# tests/test_ingest.py; waiting 3 real minutes here would slow the suite down).
STALE_FETCH = """(() => { const f = window.fetch; window.fetch = async (...a) => {
  const r = await f(...a); if (!String(a[0]).includes('/api/overview')) return r;
  const d = await r.json(); d.status.lastReadingAgeSeconds = 420; d.status.state = 'stale';
  return new Response(JSON.stringify(d), {status: 200, headers: {'Content-Type': 'application/json'}}); }; })();"""


@pytest.mark.parametrize('size', [(1366, 768), (1600, 900)])
def test_live_mode_indicator_and_stale_feed_banner(size):
    server = Server('--mode', 'live').start('waiting')
    try:
        with Browser(*size) as b:
            b.goto(server.base + '/')
            b.wait_for("document.getElementById('live').textContent === 'Waiting for readings…'", 15)
            assert 'Waiting for the first readings' in b.eval("document.getElementById('rows').textContent")
            assert 'gateways' in b.eval("document.getElementById('live').title")
            result = _push(server, _readings(0, 40))           # caustic runs 39 min: over its limit
            assert result['accepted'] == 40
            b.wait_for("document.getElementById('live').textContent.startsWith('Live · ')", 10)
            b.wait_for("document.querySelectorAll('tr.alertrow').length === 1", 10)
            assert 'ok' in b.eval("document.getElementById('live').className")
            assert re.fullmatch(r'Live · last reading \d+ s ago', b.eval("document.getElementById('live').textContent"))
            assert 'Replay' not in b.eval("document.getElementById('live').title")
            assert b.eval("document.getElementById('banner').hidden")
            assert b.errors == []
        with Browser(*size) as b:
            b.send('Page.addScriptToEvaluateOnNewDocument', source=STALE_FETCH)
            b.goto(server.base + '/')
            b.wait_for("document.getElementById('live').classList.contains('warn')", 15)
            live = b.eval("document.getElementById('live').textContent")
            assert live == 'Stale · no reading for 7 min', live
            assert b.eval("getComputedStyle(document.getElementById('live')).color") == 'rgb(163, 84, 8)'
            banner = b.eval("document.getElementById('banner').textContent")
            assert not b.eval("document.getElementById('banner').hidden")
            assert 'No readings received for 7 min' in banner and '24 Jul 09:39' in banner
            assert b.eval("document.body.classList.contains('feedstale') && !document.body.classList.contains('stale')")
            assert not b.eval("document.getElementById('period').disabled"), 'the server is reachable: filters work'
            assert not b.eval("document.getElementById('ackBtn').disabled"), 'stale data can still be acknowledged'
            top = b.eval("document.querySelector('.top').getBoundingClientRect().right")
            right = b.eval("document.querySelector('.right').getBoundingClientRect().right")
            assert right <= top + 1, 'the live status must not push the top bar off screen'
            assert b.errors == []
    finally:
        server.stop()


# ================================================================ proposed T/A/C rules (pending Danone sign-off) and links
@pytest.fixture(scope='module')
def ended_alert_mode():
    server = Server('--speed', '10000000', env={'CIP_PROPOSED_RULES': 'alert'}).start('finished')
    yield server
    server.stop()


TILES = """[...document.querySelectorAll('.tact .p')].map(p => {
    const st = p.querySelector('.st'), lim = p.querySelector('.lim'), tag = st.querySelector('.ptag');
    return {name: p.querySelector('.name').textContent, cls: p.className, st: st.textContent, lim: lim.textContent,
            tip: p.title, tag: tag ? tag.textContent : null, tagTitle: tag ? tag.title : null,
            tagOutline: tag ? getComputedStyle(tag).outlineStyle : null,
            plimit: p.querySelectorAll('polyline.plimit').length,
            over: st.scrollWidth > st.clientWidth + 1 || lim.scrollWidth > lim.clientWidth + 1};
})"""


def _select(b: Browser, alert_id: str):
    b.eval(f'select({json.dumps(alert_id)})')
    b.wait_for(f"state.detail && state.detail.alert.id === {json.dumps(alert_id)} && document.querySelector('#detail h2')", 10)
    time.sleep(0.2)


@pytest.mark.parametrize('size', [(1366, 768), (1600, 900)])
def test_show_mode_tiles_carry_the_proposed_evaluation(browser, ended, size):
    browser.viewport(*size)
    open_page(browser, ended, period='all')
    assert ended.get('/api/status')['proposedRules']['mode'] == 'show'
    # a caustic alert raised during circulation, where the proposed rules check temperature, flow and concentration
    caustic = browser.eval("""state.overview.objects.flatMap(o => o.alerts).filter(a => a.phase === 'Caustic'
        && a.tact.some(p => p.proposed && p.proposed.status !== 'not_checked')).map(a => a.id)""")
    _select(browser, caustic[0])
    tiles = browser.eval(TILES)
    proposed = [t for t in tiles if t['tag']]
    assert {t['name'] for t in proposed} <= {'Temperature', 'Action', 'Concentration'} and proposed
    for t in proposed:
        assert t['tag'] == 'Proposed' and t['tagOutline'] == 'dashed'
        assert 'pending Danone sign-off' in t['tagTitle'] and 'pending Danone sign-off' in t['tip']
        assert t['lim'].startswith(('Within limit ', 'Below limit ')), t['lim']
        assert ('pout' in t['cls']) == t['lim'].startswith('Below'), t
        assert 'breach' not in t['cls'], 'show mode never presents a proposed result as an alert'
    assert any(t['plimit'] for t in proposed), 'the proposed limit is drawn on the trend line'
    assert not [t for t in tiles if t['over']], [t for t in tiles if t['over']]
    assert browser.eval("document.querySelectorAll('.val.proposed, #detail .pnote').length") == 0
    assert browser.eval("document.getElementById('stats').querySelector('.v').textContent") == '221'


@pytest.mark.parametrize('size', [(1366, 768), (1600, 900)])
def test_alert_mode_proposed_alert_row_and_panel(browser, ended_alert_mode, size):
    browser.viewport(*size)
    open_page(browser, ended_alert_mode, period='all')
    row = browser.eval("""(() => { const r = [...document.querySelectorAll('tr.alertrow')].find(r => r.querySelector('.val.proposed'));
        return r && {id: r.dataset.id, title: r.querySelector('.alert').textContent, sub: r.querySelector('.alert-sub').textContent,
                     outline: getComputedStyle(r.querySelector('.val.proposed')).outlineStyle,
                     pill: r.querySelector('.val.proposed').title}; })()""")
    assert row, 'a row whose latest alert comes from a proposed rule'
    assert row['title'].endswith(' low') and 'Proposed rule' in row['sub'] and row['outline'] == 'dashed'
    assert 'pending Danone sign-off' in row['pill']
    heights = browser.eval("[...document.querySelectorAll('tr.alertrow')].map(r => Math.round(r.getBoundingClientRect().height))")
    assert max(heights) - min(heights) <= 1, 'proposed rows keep the two-line row height'
    browser.click(f'tr.alertrow[data-id="{row["id"]}"]')
    browser.wait_for(f"state.detail && state.detail.alert.id === {json.dumps(row['id'])}", 10)
    time.sleep(0.3)
    panel = browser.eval("""(() => ({h2: document.querySelector('#detail h2').textContent, tag: document.querySelector('#detail .tag').textContent,
        why: document.querySelector('#detail .why').textContent, param: state.detail.alert.parameter,
        width: [document.getElementById('detail').scrollWidth, document.getElementById('detail').clientWidth]}))()""")
    assert panel['h2'].endswith('below proposed limit') and 'Proposed rule' in panel['tag']
    assert 'stayed below the proposed limit' in panel['why'] and 'pending Danone sign-off' in panel['why']
    tiles = {t['name']: t for t in browser.eval(TILES)}
    own = tiles[panel['param']]
    assert 'pbreach' in own['cls'] and own['lim'].startswith('Below limit ')
    assert tiles['Time']['st'] != 'Out of limit' or 'breach' in tiles['Time']['cls']
    assert not [t for t in tiles.values() if t['over']]
    assert panel['width'][0] <= panel['width'][1]


def test_notification_link_opens_that_alert(browser, ended_alert_mode):
    overview = ended_alert_mode.get('/api/overview?period=all')
    old = min((a for o in overview['objects'] for a in o['alerts'] if a['proposed']), key=lambda a: a['raisedAt'])
    browser.goto(ended_alert_mode.base + '/#alert=' + quote(old['id'], safe=''))
    browser.wait_for(f"state.detail && state.detail.alert.id === {json.dumps(old['id'])} && document.querySelector('#detail h2')", 30)
    wait_for_poll(browser)                                    # still shown after a poll, although outside 24 h
    assert browser.eval('state.selectedId') == old['id'] and browser.eval('location.hash') == ''
    assert browser.eval("document.querySelector('#detail h2').textContent").endswith('below proposed limit')
    browser.eval("location.hash = '#alert=' + encodeURIComponent('nope:missing')")
    browser.wait_for("state.selectedId && state.selectedId !== 'nope:missing'", 15)   # unknown id: back to the newest


# ================================================================ acknowledge popover, audit log, run history, system status
@pytest.fixture(scope='module')
def ended_ack():
    """Own server for tests that acknowledge alerts. A Teams webhook is configured (replay mode never sends)."""
    server = Server('--speed', '10000000', env={'CIP_TEAMS_WEBHOOK': 'http://127.0.0.1:9/hook'}).start('finished')
    yield server
    server.stop()


def _unacked_ids(b: Browser) -> list[str]:
    return b.eval("state.overview.objects.flatMap(o => o.alerts).filter(a => !a.acknowledgedAt).map(a => a.id)")


def _in_viewport(b: Browser, selector: str) -> bool:
    return b.eval(f"""(() => {{ const r = document.querySelector({json.dumps(selector)}).getBoundingClientRect();
        return r.width > 0 && r.left >= 0 && r.top >= 0 && r.right <= innerWidth && r.bottom <= innerHeight; }})()""")


@pytest.mark.parametrize('size', [(1366, 768), (1600, 900)])
def test_acknowledge_asks_name_once_and_shows_audit_trail(ended_ack, size):
    with Browser(*size) as b:
        open_page(b, ended_ack)
        alert_id = b.eval('state.selectedId')
        b.click('#ackBtn')
        b.wait_for("!document.getElementById('ackPop').hidden && document.activeElement.id === 'apName'", 5)
        assert b.eval("document.getElementById('ackBtn').getAttribute('aria-expanded')") == 'true'
        assert _in_viewport(b, '#ackPop')
        pop, btn = b.eval("[document.getElementById('ackPop').getBoundingClientRect().bottom, "
                          "document.getElementById('ackBtn').getBoundingClientRect().top]")
        assert pop <= btn, 'the popover sits above the Acknowledge button'
        b.eval("document.getElementById('ackForm').requestSubmit()")       # no name: refused in the page
        b.wait_for("!document.getElementById('apErr').hidden", 5)
        assert b.eval("document.getElementById('apName').getAttribute('aria-invalid')") == 'true'
        assert not ended_ack.get('/api/alerts/' + quote(alert_id, safe=''))['alert']['acknowledgedAt']
        b.key('Escape', key_code=27)
        b.wait_for("document.getElementById('ackPop').hidden && document.activeElement.id === 'ackBtn'", 5)

        note = 'Checked caustic valve V-12; steam supply was low at shift change.'
        acknowledge(b, 'Priya Sharma', note)
        wait_for_poll(b)
        trail = b.eval("[...document.querySelectorAll('#detail .trail li')].map(li => li.textContent)")
        assert trail[0].startswith('Alert raised by the monitor')
        assert trail[-1].startswith('Acknowledged by Priya Sharma') and note in trail[-1]
        assert 'Priya Sharma' in b.eval("document.getElementById('ackLine').textContent")
        alert = ended_ack.get('/api/alerts/' + quote(alert_id, safe=''))['alert']
        assert alert['acknowledgedBy'] == 'Priya Sharma' and alert['acknowledgementNote'] == note
        assert b.eval("localStorage.getItem('cip.operatorName')") == 'Priya Sharma'
        assert b.eval("document.getElementById('detail').scrollWidth <= document.getElementById('detail').clientWidth")

        # the next acknowledgement only asks for a note; the name can still be changed
        _select(b, _unacked_ids(b)[0])
        b.click('#ackBtn')
        b.wait_for("!document.getElementById('ackPop').hidden && document.activeElement.id === 'apNote'", 5)
        assert b.eval("document.getElementById('apNameField').hidden && !document.getElementById('apWho').hidden")
        assert b.eval("document.getElementById('apWhoName').textContent") == 'Priya Sharma'
        assert not b.eval("document.getElementById('ackPop').querySelector('.ap-head b').getBoundingClientRect().height > 24"), \
            'the popover title stays on one line'
        b.click('#apChange')
        assert b.eval("!document.getElementById('apNameField').hidden && document.getElementById('apName').value") == 'Priya Sharma'
        b.click('#apCancel')
        b.wait_for("document.getElementById('ackPop').hidden", 5)
        assert b.eval("document.scrollingElement.scrollTop") == 0, 'the page itself never scrolls'
        assert b.errors == []


@pytest.mark.parametrize('size', [(1366, 768), (1600, 900)])
def test_audit_log_view_filters_exports_and_links(ended_ack, size):
    acked = next(a for o in ended_ack.get('/api/overview?period=all')['objects'] for a in o['alerts'] if not a['acknowledgedAt'])
    ended_ack.post('/api/alerts/' + quote(acked['id'], safe='') + '/acknowledge',
                   json.dumps({'by': f'Audit {size[0]}', 'note': 'seen in audit test'}).encode())
    with Browser(*size) as b:
        open_page(b, ended_ack)
        b.click('.nav a[data-view="audit"]')
        b.wait_for("location.hash === '#audit' && state.audit && document.querySelectorAll('#audRows tr .ev').length > 0", 15)
        assert b.eval("document.getElementById('viewOverview').hidden && !document.getElementById('viewAudit').hidden")
        assert b.eval("document.querySelector('.nav a[aria-current]').textContent") == 'Audit log'
        assert b.eval("document.getElementById('periodWrap').hidden"), 'the audit log is the full record, not cut by period'
        total = ended_ack.get('/api/audit?limit=1')['total']
        assert b.eval("document.getElementById('audCount').textContent") == f'Latest 100 of {total:,} entries'
        assert b.eval("document.querySelectorAll('#audRows tr').length") == 100

        b.eval("(() => { const s = document.getElementById('audType'); s.value = 'acknowledged'; s.dispatchEvent(new Event('change')); })()")
        b.wait_for("state.audit && state.audType === 'acknowledged' && document.querySelectorAll('#audRows tr .ev').length > 0", 10)
        events = b.eval("[...document.querySelectorAll('#audRows .ev')].map(e => e.textContent)")
        acks = ended_ack.get('/api/audit?event=acknowledged&limit=1000')
        assert set(events) == {'Acknowledged'} and len(events) == acks['total']
        row = b.eval(f"""[...document.querySelectorAll('#audRows tr')].map(r => r.textContent).find(t => t.includes('Audit {size[0]}'))""")
        assert row and 'seen in audit test' in row

        b.eval(f"(() => {{ const s = document.getElementById('audObj'); s.value = {json.dumps(acked['objectId'])};"
               " s.dispatchEvent(new Event('change')); })()")
        b.wait_for(f"state.audit && state.audObj === {json.dumps(acked['objectId'])} && !document.querySelector('#audRows .empty')", 10)
        codes = b.eval("[...document.querySelectorAll('#audRows tr .obj')].map(e => e.textContent)")
        assert codes and set(codes) == {re.match(r'\s*([A-Za-z0-9_]+)', acked['objectId']).group(1)}

        href = b.eval("document.getElementById('audCsv').href")
        assert 'event=acknowledged' in href and 'object=' in href
        with urllib.request.urlopen(href, timeout=10) as r:
            body = r.read().decode('utf-8-sig')
            assert r.status == 200 and r.headers['Content-Type'].startswith('text/csv')
            assert 'attachment' in r.headers['Content-Disposition']
        assert f'Audit {size[0]}' in body and all(',acknowledged,' in line for line in body.strip().splitlines()[1:])
        assert b.eval("document.querySelector('.vt').getBoundingClientRect().right <= innerWidth")
        assert b.eval("[...document.querySelectorAll('#audRows td')].every(td => td.scrollWidth <= td.clientWidth + 1)")

        b.click('#audRows .alink')                       # opens the alert in the overview
        b.wait_for(f"state.view === 'overview' && state.selectedId === {json.dumps(acked['id'])} "
                   "&& state.detail && state.detail.alert.id === state.selectedId", 15)
        assert not b.eval("document.getElementById('viewOverview').hidden")
        assert b.errors == []


@pytest.mark.parametrize('size', [(1366, 768), (1600, 900)])
def test_run_history_view_lists_runs_and_opens_them(browser, ended, size):
    browser.viewport(*size)
    open_page(browser, ended)
    browser.click('.nav a[data-view="runs"]')
    browser.wait_for("location.hash === '#runs' && state.runs && document.querySelectorAll('#runRows tr.click').length > 0", 15)
    runs = ended.get('/api/runs?period=24h&limit=1000&summary=0')['runs']
    stats = browser.eval("[...document.querySelectorAll('#runStats .v')].map(e => parseInt(e.textContent.replace(/,/g, ''), 10))")
    assert stats == [len(runs), sum(r['status'] == 'ALERT' for r in runs), sum(r['status'] == 'OK' for r in runs),
                     sum(r['status'] == 'NO REFERENCE' for r in runs)]
    assert browser.eval("document.querySelectorAll('#runRows tr.click').length") == min(100, len(runs))
    assert browser.eval("[...document.querySelectorAll('#runRows td')].every(td => td.scrollWidth <= td.clientWidth + 1)")
    assert browser.eval("document.querySelector('.vt').getBoundingClientRect().right <= innerWidth")

    browser.eval("(() => { const s = document.getElementById('runStatus'); s.value = 'ALERT'; s.dispatchEvent(new Event('change')); })()")
    pills = browser.eval("[...document.querySelectorAll('#runRows tr.click .pill')].map(p => p.textContent)")
    assert pills and set(pills) == {'Alert raised'}
    over = browser.eval("[...document.querySelectorAll('#runRows .mres')].map(e => [e.className, e.textContent])")
    assert all('over' in c and ' ran ' in t and 'limit' in t for c, t in over)

    key = browser.eval("""[...document.querySelectorAll('#runRows tr.click')].find(r => r.querySelector('.dot.open')).dataset.key""")
    browser.eval(f"document.querySelector('#runRows tr[data-key=\"' + CSS.escape({json.dumps(key)}) + '\"]').focus()")
    press_enter(browser)
    browser.wait_for("document.getElementById('run').open && document.querySelector('#runBody .alist')", 5)
    body = browser.eval("document.getElementById('runBody').textContent")
    assert 'Alerts in this run' in body and 'Activity times' in body and 'Over limit' in body
    browser.key('Escape', key_code=27)
    browser.wait_for("!document.getElementById('run').open", 5)
    assert browser.eval("document.activeElement.dataset.key") == key, 'focus returns to the run row'
    press_enter(browser)
    browser.wait_for("document.getElementById('run').open", 5)
    alert_id = browser.eval("document.querySelector('#runBody [data-open-alert]').dataset.openAlert")
    browser.click('#runBody [data-open-alert]')
    browser.wait_for(f"!document.getElementById('run').open && state.view === 'overview' && state.selectedId === {json.dumps(alert_id)}"
                     " && state.detail && state.detail.alert.id === state.selectedId", 15)
    assert browser.errors == []


def test_nav_has_no_dead_links(browser, ended):
    open_page(browser, ended)
    nav = browser.eval("[...document.querySelectorAll('.nav a')].map(a => [a.textContent, a.getAttribute('href'), a.getAttribute('aria-disabled')])")
    working = [n for n in nav if n[1] is not None]
    assert [n[0] for n in working] == ['Overview', 'Run history', 'Audit log']
    assert all(n[2] == 'true' and 'Soon' in n[0] for n in nav if n[1] is None), 'unbuilt pages are marked, not linked'
    for view, heading in (('runs', 'Run history'), ('audit', 'Audit log'), ('overview', 'Overview')):
        browser.click(f'.nav a[data-view="{view}"]')
        browser.wait_for(f"state.view === '{view}'", 5)
        assert browser.eval("document.title") == f'CIP Monitor · {heading}'


@pytest.mark.parametrize('width', [1180, 1280, 1366, 1440, 1600, 1920])
def test_top_bar_fits_with_system_button(browser, ended, width):
    browser.viewport(width, 800)
    open_page(browser, ended)
    fit = browser.eval("""(() => { const t = document.querySelector('.top'), s = document.getElementById('sysBtn').getBoundingClientRect();
        return {over: t.scrollWidth - t.clientWidth, sys: s.right <= innerWidth - 16 && s.width > 30,
                page: document.scrollingElement.scrollHeight - innerHeight,
                nav: [...document.querySelectorAll('.nav a[data-view]')].every(a => a.getBoundingClientRect().width > 40)}; })()""")
    assert fit == {'over': 0, 'sys': True, 'page': 0, 'nav': True}, fit


def test_system_status_popover(browser, ended, ended_ack):
    open_page(browser, ended)
    assert 'Notifications off' in browser.eval("document.getElementById('sysBtn').title")
    browser.click('#sysBtn')
    browser.wait_for("!document.getElementById('sysPop').hidden", 5)
    text = browser.eval("document.getElementById('sysPop').textContent")
    assert 'Microsoft TeamsNot set up' in text and 'EmailNot set up' in text
    assert 'Kept in the monitor’s database' in text and 'pending Danone sign-off' in text and 'Replay of the July 2026 logs' in text
    assert _in_viewport(browser, '#sysPop')
    browser.key('Escape', key_code=27)
    browser.wait_for("document.getElementById('sysPop').hidden && document.activeElement.id === 'sysBtn'", 5)

    open_page(browser, ended_ack)                         # a Teams webhook is set up, but a replay never sends
    assert 'Notifications set up (Teams), paused during the replay' in browser.eval("document.getElementById('sysBtn').title")
    browser.click('#sysBtn')
    browser.wait_for("!document.getElementById('sysPop').hidden", 5)
    text = browser.eval("document.getElementById('sysPop').textContent")
    assert 'Microsoft Teams0 sent' in text and 'Not sent while the log files are replayed' in text
    assert '127.0.0.1:9' not in text, 'the webhook URL is a secret'
    browser.click('h1')
    browser.wait_for("document.getElementById('sysPop').hidden", 5)
    assert browser.errors == []


def test_live_mode_turns_stale_from_the_server():
    """A real stale feed (no faked fetch): the server's stale threshold is lowered to 2 s."""
    server = Server('--mode', 'live', env={'CIP_STALE_AFTER_SECONDS': '2'}).start('waiting')
    try:
        with Browser(1366, 768) as b:
            b.goto(server.base + '/')
            b.wait_for("document.getElementById('live').textContent === 'Waiting for readings…'", 15)
            _push(server, _readings(0, 10))
            b.wait_for("document.getElementById('live').textContent.startsWith('Live · last reading')", 10)
            b.wait_for("document.getElementById('live').textContent.startsWith('Stale · no reading for')", 15)
            assert server.get('/api/status')['state'] == 'stale'
            assert b.eval("getComputedStyle(document.getElementById('live').querySelector('i')).backgroundColor") == 'rgb(240, 160, 48)'
            assert not b.eval("document.getElementById('banner').hidden")
            assert 'Replay' not in b.eval("document.getElementById('live').title + document.getElementById('banner').textContent")
            _push(server, _readings(10, 1))                 # a new reading: live again
            b.wait_for("document.getElementById('live').textContent.startsWith('Live · last reading')", 10)
            assert b.errors == []
    finally:
        server.stop()

"""Minimal headless-Chrome driver over the DevTools protocol (no Selenium/Playwright needed).

    from cdp import Browser
    with Browser(width=1600, height=900) as b:
        b.goto('http://127.0.0.1:8000/')
        b.wait_for("document.querySelectorAll('tr.alertrow').length > 0")
        b.click('tr.alertrow:nth-child(2)')
        b.screenshot('out.png')
        print(b.eval('document.title'))

Requires Google Chrome and the ``websocket-client`` package.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request

import websocket

CHROME_CANDIDATES = [
    os.environ.get('CHROME_PATH', ''),
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser',
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Browser:
    def __init__(self, width: int = 1600, height: int = 900, scale: float = 1):
        self.width, self.height, self.scale = width, height, scale
        self.chrome = next((p for p in CHROME_CANDIDATES if p and os.path.exists(p)), None)
        if not self.chrome:
            raise RuntimeError('Chrome not found; set CHROME_PATH')
        self._id = 0
        self.console: list[str] = []
        self.errors: list[str] = []

    def __enter__(self):
        self.profile = tempfile.mkdtemp(prefix='cdp-')
        self.port = _free_port()
        self.proc = subprocess.Popen(
            [self.chrome, '--headless=new', '--disable-gpu', '--hide-scrollbars', '--no-first-run',
             '--no-default-browser-check', f'--remote-debugging-port={self.port}',
             f'--user-data-dir={self.profile}', f'--window-size={self.width},{self.height}', 'about:blank'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ws_url = None
        for _ in range(100):
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{self.port}/json') as r:
                    pages = [t for t in json.loads(r.read()) if t.get('type') == 'page']
                if pages:
                    ws_url = pages[0]['webSocketDebuggerUrl']
                    break
            except OSError:
                pass
            time.sleep(0.1)
        if not ws_url:
            raise RuntimeError('Chrome did not start')
        self.ws = websocket.create_connection(ws_url, timeout=30)
        for domain in ('Page', 'Runtime', 'Log'):
            self.send(f'{domain}.enable')
        self.viewport(self.width, self.height, self.scale)
        return self

    def __exit__(self, *exc):
        try:
            self.ws.close()
        finally:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            shutil.rmtree(self.profile, ignore_errors=True)

    # ------------------------------------------------------------ protocol
    def send(self, method: str, **params):
        self._id += 1
        msg_id = self._id
        self.ws.send(json.dumps({'id': msg_id, 'method': method, 'params': params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get('id') == msg_id:
                if 'error' in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get('result', {})
            self._event(msg)

    def _event(self, msg):
        method = msg.get('method')
        if method == 'Runtime.consoleAPICalled':
            args = ' '.join(str(a.get('value', a.get('description', ''))) for a in msg['params']['args'])
            self.console.append(f"{msg['params']['type']}: {args}")
        elif method == 'Runtime.exceptionThrown':
            d = msg['params']['exceptionDetails']
            self.errors.append(d.get('exception', {}).get('description') or d.get('text'))
        elif method == 'Log.entryAdded' and msg['params']['entry']['level'] == 'error':
            self.errors.append(msg['params']['entry']['text'])

    # ------------------------------------------------------------ helpers
    def viewport(self, width: int, height: int, scale: float = 1):
        self.width, self.height = width, height
        self.send('Emulation.setDeviceMetricsOverride', width=width, height=height,
                  deviceScaleFactor=scale, mobile=False)

    def goto(self, url: str, settle: float = 0.5):
        self.send('Page.navigate', url=url)
        self.wait_for("document.readyState === 'complete'")
        time.sleep(settle)

    def eval(self, expression: str):
        """Evaluate JS (awaits promises) and return the JSON value."""
        result = self.send('Runtime.evaluate', expression=expression, returnByValue=True, awaitPromise=True)
        if 'exceptionDetails' in result:
            raise RuntimeError(result['exceptionDetails'].get('exception', {}).get('description')
                               or result['exceptionDetails'].get('text'))
        return result.get('result', {}).get('value')

    def wait_for(self, expression: str, timeout: float = 15):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if self.eval(f'!!({expression})'):
                    return True
            except RuntimeError:
                pass
            time.sleep(0.1)
        raise TimeoutError(expression)

    def click(self, selector: str):
        """Real mouse click at the element's centre."""
        box = self.eval(f"""(() => {{ const e = document.querySelector({json.dumps(selector)});
            if (!e) return null; e.scrollIntoView({{block:'center'}}); const r = e.getBoundingClientRect();
            return [r.x + r.width/2, r.y + r.height/2]; }})()""")
        if box is None:
            raise LookupError(selector)
        for kind in ('mousePressed', 'mouseReleased'):
            self.send('Input.dispatchMouseEvent', type=kind, x=box[0], y=box[1], button='left', clickCount=1)

    def key(self, key: str, code: str | None = None, key_code: int | None = None):
        params = {'key': key, 'code': code or key}
        if key_code:
            params['windowsVirtualKeyCode'] = key_code
        self.send('Input.dispatchKeyEvent', type='keyDown', **params)
        self.send('Input.dispatchKeyEvent', type='keyUp', **params)

    def screenshot(self, path: str, full_page: bool = False):
        params = {'format': 'png'}
        if full_page:
            params['captureBeyondViewport'] = True
        data = self.send('Page.captureScreenshot', **params)['data']
        with open(path, 'wb') as f:
            f.write(base64.b64decode(data))
        return path

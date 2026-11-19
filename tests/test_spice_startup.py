"""Browser startup regression with a stale, unversioned clipboard module.

RUN_BROWSER_TESTS=1 python -m pytest tests/test_spice_startup.py -q
"""
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import pytest
from flask import render_template

import main

pytestmark = pytest.mark.skipif(os.environ.get('RUN_BROWSER_TESTS') != '1', reason='opt-in browser test')


@pytest.mark.parametrize('legacy_template', [False, True], ids=['current-template', 'older-template'])
def test_console_bypasses_stale_clipboard_module_and_initializes_both_drawers(legacy_template):
    from playwright.sync_api import sync_playwright, expect

    root = Path(main.app.root_path)
    with main.app.test_request_context():
        html = render_template('spice_console.html', vmid=101, csrf='test-csrf',
                               fallback='', file_upload_enabled=False, file_download_enabled=False)
    if legacy_template:
        # Reproduce a running Flask process still holding the template from
        # before clipboard guidance/quotas while bind-mounted JS is updated.
        html = re.sub(r'<details id="clipboard-access-help">.*?</details>', '', html, flags=re.S)
        html = re.sub(r'<div id="clipboard-remaining"[^>]*>.*?</div>', '', html, flags=re.S)
        html = re.sub(r'<p id="clipboard-policy-status"[^>]*>.*?</p>', '', html, flags=re.S)
        html = re.sub(r'<button id="clipboard-receive-button"[^>]*>.*?</button>', '', html, flags=re.S)
        html = html.replace('id="clipboard-auto-controls"', '')
    errors = []
    stale_requests = []
    session_requests = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**(
            {'executable_path': os.environ['PLAYWRIGHT_CHROMIUM_PATH']}
            if os.environ.get('PLAYWRIGHT_CHROMIUM_PATH') else {}))
        try:
            page = browser.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))

            def serve(route):
                url = urlparse(route.request.url)
                if url.path == '/console':
                    route.fulfill(body=html, content_type='text/html')
                elif url.path == '/static/spice-clipboard-sync.js' and not url.query:
                    # A pre-update cached module lacks clipboardAccessHelp. Its
                    # named import aborts the entire console before menus bind.
                    stale_requests.append(route.request.url)
                    route.fulfill(body='export class ClipboardSync {}', content_type='text/javascript')
                elif url.path.startswith('/static/'):
                    resource = root / url.path.lstrip('/')
                    route.fulfill(body=resource.read_bytes(), content_type=(
                        'text/javascript' if resource.suffix == '.js' else 'text/css'))
                else:
                    session_requests.append(url.path)
                    route.fulfill(status=503, body=json.dumps({'error': 'Simulated VM unavailable'}),
                                  content_type='application/json')

            page.route('http://spice.test/**', serve)
            page.goto('http://spice.test/console')
            expect(page.locator('#status')).to_have_text('Simulated VM unavailable')
            page.locator('#controls-toggle').click()
            expect(page.locator('#console-controls')).to_be_visible()
            page.locator('#clipboard-toggle').click()
            expect(page.locator('#clipboard-panel')).to_be_visible()
            assert session_requests == ['/api/spice/101/session']
            assert errors == []
            assert stale_requests == []
        finally:
            browser.close()

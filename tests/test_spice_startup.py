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
        html = re.sub(r'<details id="vm-login-details"[^>]*>.*?</details>', '', html, flags=re.S)
        html = html.replace('id="clipboard-auto-controls"', '')
    errors = []
    stale_requests = []
    session_requests = []
    login_requests = []
    guest_password = ' long-unbroken-password-for-the-narrow-drawer <em>with markup</em> '
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
                elif url.path == '/api/spice/101/login-details':
                    login_requests.append(url.path)
                    credentials = {'username': 'guest-user', 'password': guest_password} if len(login_requests) < 3 else {}
                    route.fulfill(json={'credentials': credentials})
                else:
                    session_requests.append(url.path)
                    route.fulfill(status=503, body=json.dumps({'error': 'Simulated VM unavailable'}),
                                  content_type='application/json')

            page.route('http://spice.test/**', serve)
            page.goto('http://spice.test/console')
            expect(page.locator('#status')).to_have_text('Simulated VM unavailable')
            drawer = page.locator('#console-controls')
            login = drawer.locator('#vm-login-details')
            if not legacy_template:
                expect(login).to_be_hidden()
            page.locator('#controls-toggle').click()
            expect(drawer).to_be_visible()
            page.locator('#clipboard-toggle').click()
            expect(page.locator('#clipboard-panel')).to_be_visible()
            if not legacy_template:
                for width in (320, 1100):
                    page.set_viewport_size({'width': width, 'height': 760})
                    assert page.locator('.clipboard-actions').evaluate_all('''rows => rows.every(row => {
                        const [first, second] = Array.from(row.children, button => button.getBoundingClientRect());
                        return first.top === second.top && second.right <= row.getBoundingClientRect().right;
                    })'''), f'Clipboard buttons wrapped at {width}px'
                page.locator('#clipboard-close').click()
                summary = login.locator('summary')
                expect(summary).to_have_accessible_name('Login details')
                expect(summary).to_be_visible()
                expect(login.locator('dl')).to_be_hidden()
                assert not login_requests
                summary.click()
                username = login.locator('[data-credential="username"]')
                expect(username).to_have_text('guest-user')
                password = login.locator('[data-credential="password"]')
                assert password.text_content() == guest_password
                expect(password.locator('em')).to_have_count(0)
                for width in (320, 1100):
                    page.set_viewport_size({'width': width, 'height': 760})
                    panel = login.locator('.vm-login-panel').bounding_box()
                    drawer_bounds = drawer.bounding_box()
                    assert drawer_bounds['x'] <= panel['x']
                    assert panel['x'] + panel['width'] <= drawer_bounds['x'] + drawer_bounds['width']
                    user_bounds, password_bounds = username.bounding_box(), password.bounding_box()
                    assert password_bounds['y'] >= user_bounds['y'] + user_bounds['height']
                    assert password_bounds['x'] + password_bounds['width'] <= panel['x'] + panel['width']
                    assert password.evaluate('field => field.scrollWidth <= field.clientWidth')
                summary.press('Escape')
                expect(password).to_be_hidden()
                expect(password).to_be_empty()
                expect(drawer).to_be_visible()
                summary.click()
                expect(username).to_have_text('guest-user')
                page.locator('#start-vm').press('Escape')
                expect(drawer).to_be_hidden()
                expect(password).to_be_empty()
                page.locator('#controls-toggle').click()
                expect(login.locator('dl')).to_be_hidden()
                assert len(login_requests) == 2
                summary.click()
                expect(login.get_by_role('status')).to_have_text('No VM login details are configured.')
                assert len(login_requests) == 3
            assert session_requests == ['/api/spice/101/session']
            assert errors == []
            assert stale_requests == []
        finally:
            browser.close()

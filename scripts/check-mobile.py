"""Render-only UI checks against synthetic API responses; never contacts Mercedes.
Build the frontend first, then: uv run --with playwright python scripts/check-mobile.py
"""
import asyncio
import json
import os
from pathlib import Path
from playwright.async_api import async_playwright

async def main():
    # An owned disposable static server; API requests are always intercepted below.
    import threading
    from functools import partial
    from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args): pass
    server = None
    origin = os.environ.get('MB_UI_TEST_ORIGIN')
    if not origin:
        server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(Path(__file__).resolve().parents[1] / 'dist')))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        origin = f'http://127.0.0.1:{server.server_port}' 
    state = {'configured': True, 'connected': False, 'quietMode': True, 'accountBlocked': False,
        'cooldownUntil': None, 'error': None, 'region': 'Europe',
        'vehicles': [{'vin': 'TEST-VEHICLE', 'name': 'Mercedes-Benz EQB', 'plate': 'Test vehicle',
            'commands': [{'id': 'charge_limit', 'chargeLimits': [50, 60, 70, 80, 90, 100]}]}],
        'schedule': {'config': {'mode': 'oneNight', 'enabled': False, 'vin': '', 'date': '2099-11-06',
            'entries': [{'time': '23:30', 'maxSoc': 80}, {'time': '05:30', 'maxSoc': 50}]},
            'timezone': 'Europe/London', 'status': 'disabled', 'message': 'Quiet. No night scheduled.', 'jobs': [], 'nextChangeAt': None}}
    async with async_playwright() as p:
        engine = os.environ.get("MB_UI_ENGINE", "chromium")
        browser = await getattr(p, engine).launch(**({"executable_path": os.environ["MB_UI_CHROMIUM"]} if engine == "chromium" and os.environ.get("MB_UI_CHROMIUM") else {}))
        page = await browser.new_page(is_mobile=True, has_touch=True, device_scale_factor=3, locale="en-GB")
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        async def route_api(route):
            path = route.request.url.split('/api')[1]
            if path == '/schedule' and route.request.method == 'PUT':
                config = route.request.post_data_json
                state['schedule'] = {'config': config, 'timezone': 'Europe/London', 'status': 'waiting',
                    'message': 'Armed for one night. Quiet until the first change.', 'jobs': [
                        {'at': config['date']+'T23:30:00Z', 'target': 80, 'status': 'scheduled', 'message': 'Waiting for its scheduled time.', 'sentAt': None, 'confirmedAt': None},
                        {'at': '2099-11-07T05:30:00Z', 'target': 50, 'status': 'scheduled', 'message': 'Waiting for its scheduled time.', 'sentAt': None, 'confirmedAt': None}], 'nextChangeAt': config['date']+'T23:30:00Z'}
            if path == '/schedule' and route.request.method == 'DELETE':
                state['schedule']['config']['enabled'] = False
                state['schedule']['status'] = 'disabled'
                state['schedule']['message'] = 'Night cancelled. Quiet; no further connections scheduled.'
                for job in state['schedule']['jobs']: job['status'] = 'cancelled'
            body = state if path == '/state' else state['schedule']
            await route.fulfill(status=200, content_type='application/json', body=json.dumps(body))
        await page.route('**/api/**', route_api)
        artifacts = Path('/tmp/mb-one-night-ui'); artifacts.mkdir(exist_ok=True)
        for width in [320, 390, 768, 1280]:
            await page.set_viewport_size({'width': width, 'height': 844})
            await page.goto(origin)
            await page.get_by_role('heading', name='Plan your charge').wait_for()
            assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), f'Overflow at {width}'
            for element in await page.locator('button:visible,input:visible,select:visible').all():
                box = await element.bounding_box()
                assert box['height'] >= 44, f'Touch target too small at {width}: {box}'
                if await element.evaluate("el => el.closest('.schedule-panel') !== null"):
                    assert await element.evaluate("""el => {
                        const r = el.getBoundingClientRect();
                        const parent = el.closest('label') || el.closest('fieldset');
                        const p = parent.getBoundingClientRect();
                        const panel = el.closest('.schedule-panel').getBoundingClientRect();
                        return r.left >= p.left - 1 && r.right <= p.right + 1 && r.right <= panel.right - 1;
                    }"""), f'Control escapes its container at {width}'
            controls = await page.locator('.step-fields input,.step-fields select').all()
            boxes = [await el.bounding_box() for el in controls]
            for i, a in enumerate(boxes):
                for b in boxes[i+1:]:
                    assert not (a['x'] < b['x']+b['width']-1 and a['x']+a['width'] > b['x']+1 and a['y'] < b['y']+b['height']-1 and a['y']+a['height'] > b['y']+1), f'Overlapping fields at {width}'
            await page.screenshot(path=str(artifacts/f'{engine}-quiet-{width}.png'), full_page=True)
        await page.set_viewport_size({'width': 390, 'height': 844})
        await page.get_by_label('Night starting').fill('2099-11-06')
        await page.get_by_label('Evening percentage').select_option('80')
        await page.get_by_role('button', name='Schedule this night').click()
        await page.get_by_text('Night armed', exact=True).wait_for()
        assert state['schedule']['config']['mode'] == 'oneNight'
        await page.screenshot(path=str(artifacts/f'{engine}-armed-390.png'), full_page=True)
        await page.get_by_role('button', name='Cancel night').click()
        await page.get_by_text('Not armed', exact=True).wait_for()
        assert not state['schedule']['config']['enabled']
        # Long safety warning and full result list must also fit a narrow screen.
        state['accountBlocked'] = True; state['cooldownUntil'] = '2099-11-07T16:10:00Z'
        await page.set_viewport_size({'width': 320, 'height': 844}); await page.reload()
        await page.get_by_text('Mercedes rate limited the last connection.', exact=False).wait_for()
        assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        await page.screenshot(path=str(artifacts/f'{engine}-cooldown-320.png'), full_page=True)
        assert not errors, errors
        await browser.close()
    if server: server.shutdown(); server.server_close()
    print(f'PASS {engine}: mobile/touch 320/390/768/1280px, containment, no overlapping controls, 44px targets, arm/cancel, cooldown, no JS errors. Synthetic API only.')

asyncio.run(main())

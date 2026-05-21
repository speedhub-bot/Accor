#!/usr/bin/env python3
"""
Accor (all.accor.com) login + full-account capture.

What it does:
  1. Opens a real Chrome window via Playwright (NOT headless — that's
     what Imperva fingerprints) with light stealth patches applied.
  2. Logs you in to your own account at https://all.accor.com/
     using the credentials you put in `.env` (or pass as flags).
  3. If hCaptcha appears, it'll try to auto-solve with
     `hcaptcha-challenger` (free, runs ONNX models locally — no API
     keys). If that's not installed or fails, the script PAUSES with
     the browser open so you can click through the challenge by hand,
     then press Enter in the terminal to continue.
  4. After login, it walks the account SPA (overview, my-bookings,
     loyalty / points, preferences, payment methods, personal data)
     and:
        - sniffs every authenticated JSON response (full bodies)
        - dumps the cookie jar (so you can replay requests later)
        - asks the page itself for the customer object, points balance,
          tier, profile fields — pulled directly from the running
          Vuex / Vue stores when available
  5. Writes everything to ./out/accor_account_<member-id>.json plus
     a sibling .raw.jsonl with every API response body for verification.

Run it:
    pip install playwright python-dotenv
    playwright install chromium                       # one-time
    pip install hcaptcha-challenger  # optional — free hCaptcha solver

    cp .env.example .env   # then edit it with your creds
    python accor_capture.py

Flags:
    --email <addr>           Override .env ACCOR_EMAIL
    --password <pw>          Override .env ACCOR_PASSWORD
    --proxy http://host:port Route the browser through a proxy (use this
                             for residential / your-home-IP traffic).
    --headed                 Force a visible window (default).
    --headless               Run headless (will likely trigger Imperva
                             — only use this AFTER you've logged in
                             once and have storage_state.json saved).
    --keep-open              Don't close the browser at the end (good
                             for poking around manually after the dump).

The script is intentionally chatty — every step prints what it's
doing, every captured endpoint is logged, and any login failure dumps
both the page HTML and the most recent screenshot to ./out/ so you
can see exactly where it broke.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import re
import sys
import time
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── third-party ──────────────────────────────────────────────
try:
    from playwright.async_api import (
        BrowserContext,
        Frame,
        Page,
        Response,
        TimeoutError as PWTimeout,
        async_playwright,
    )
except ImportError:
    print("playwright not installed. Run: pip install playwright && playwright install chromium",
          file=sys.stderr)
    sys.exit(1)

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    # python-dotenv is optional — the script still works with raw env vars.
    pass

# hcaptcha-challenger is OPTIONAL. Import lazily so it's only loaded
# when we actually face a challenge.
_HC_AVAILABLE: Optional[bool] = None


# ── configuration ─────────────────────────────────────────────
ACCOR_HOME = "https://all.accor.com/a/en.html"
ACCOR_ACCOUNT = "https://all.accor.com/account/en/my-bookings"
CUSTOMER_API = "https://all.accor.com/content/sling/servlets/ace/customer"

# URLs that signal "yes, we have a logged-in session" once a 2xx body
# comes back through them.
_LOGIN_OK_URLS = (
    CUSTOMER_API,
    "/account/en/",
    "/api/customer",
    "/contact-center/v2/contact",
)

# JSON-like API endpoints whose bodies we want to keep verbatim.
_CAPTURE_PATTERNS = (
    "api.accor.com",
    "aem-api.accor.com",
    "all.accor.com/content/sling/servlets",
    "all.accor.com/account/api",
    "/loyalty",
    "/rewards",
    "/bookings",
    "/customer",
    "/contact",
    "/preferences",
    "/payment",
)

OUT_DIR = Path("./out")


# ── utility ──────────────────────────────────────────────────


def _now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(msg: str) -> None:
    print(f"[{_now()}] {msg}", flush=True)


def _safe_write(path: Path, payload: Any) -> None:
    """Write *payload* as pretty JSON (or as-is for str/bytes)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, (dict, list)):
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    elif isinstance(payload, bytes):
        path.write_bytes(payload)
    else:
        path.write_text(str(payload))


def _slugify(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value).strip("_") or "unknown"


# ── stealth patches (light — we're not impersonating, just avoiding
# the most obvious headless tells) ────────────────────────────


_STEALTH_JS = r"""
// hide webdriver
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
// languages / plugins (Imperva inspects these)
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', {
    get: () => [
        { name: 'Chrome PDF Plugin' },
        { name: 'Chrome PDF Viewer' },
        { name: 'Native Client' },
    ],
});
// chrome runtime — present in real Chrome, missing in stock CDP.
window.chrome = { runtime: {} };
// permissions query (real Chrome returns 'prompt' for notifications;
// CDP returns 'denied' by default which is a giveaway).
const _origQuery = window.navigator.permissions.query.bind(window.navigator.permissions);
window.navigator.permissions.query = (params) =>
    params && params.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : _origQuery(params);
"""


# ── captured-API plumbing ────────────────────────────────────


@dataclasses.dataclass
class CapturedResponse:
    url: str
    method: str
    status: int
    content_type: str
    body: Any  # parsed JSON when possible, otherwise raw text


class APIBag:
    """Collects every JSON response that matches our capture patterns.
    Deduplicated by (method, url-without-query)."""

    def __init__(self) -> None:
        self.items: Dict[tuple, CapturedResponse] = {}

    def offer(self, item: CapturedResponse) -> None:
        key = (item.method, item.url.split("?", 1)[0])
        # Prefer the LATEST body for the same key — auth'd responses
        # often arrive after an unauth'd 401, and we want the good one.
        self.items[key] = item

    def to_list(self) -> List[Dict[str, Any]]:
        return [dataclasses.asdict(v) for v in self.items.values()]


async def _wire_response_capture(ctx: BrowserContext, bag: APIBag, raw_log: Path) -> None:
    """Subscribe to every response in the context and squirrel the
    interesting ones away (both into the in-memory bag and into a
    line-delimited JSONL audit log on disk)."""

    raw_log.parent.mkdir(parents=True, exist_ok=True)
    raw_fh = open(raw_log, "w")

    async def on_response(resp: Response) -> None:
        url = resp.url
        if not any(p in url for p in _CAPTURE_PATTERNS):
            return
        ctype = (resp.headers.get("content-type") or "").lower()
        body: Any = None
        try:
            if "json" in ctype:
                body = await resp.json()
            else:
                # Best-effort text capture (skip binary).
                if any(x in ctype for x in ("text", "javascript", "xml", "html")):
                    body = await resp.text()
        except Exception as exc:
            body = f"<<read error: {exc}>>"
        item = CapturedResponse(
            url=url,
            method=resp.request.method,
            status=resp.status,
            content_type=ctype,
            body=body,
        )
        bag.offer(item)
        try:
            raw_fh.write(
                json.dumps(
                    {
                        "ts": _now(),
                        **dataclasses.asdict(item),
                    },
                    ensure_ascii=False,
                    default=str,
                ) + "\n"
            )
            raw_fh.flush()
        except Exception:
            pass

    ctx.on("response", lambda r: asyncio.create_task(on_response(r)))


# ── hCaptcha handling ────────────────────────────────────────


def _hcaptcha_available() -> bool:
    global _HC_AVAILABLE
    if _HC_AVAILABLE is None:
        try:
            import hcaptcha_challenger  # noqa: F401
            _HC_AVAILABLE = True
        except ImportError:
            _HC_AVAILABLE = False
    return _HC_AVAILABLE


async def _find_hcaptcha_frame(page: Page) -> Optional[Frame]:
    """Return the inner hCaptcha challenge frame if it's currently
    rendered, otherwise None. hCaptcha renders two iframes — the outer
    'checkbox' frame (src contains 'newassets.hcaptcha.com' or
    'hcaptcha.com/captcha/v1') and the inner 'challenge' frame that
    appears after the checkbox is clicked."""
    for fr in page.frames:
        src = fr.url or ""
        if "hcaptcha.com" in src and ("challenge" in src or "hcaptcha-checkbox" in src):
            return fr
    return None


async def _try_solve_hcaptcha(page: Page) -> bool:
    """Attempt to auto-solve via hcaptcha-challenger. Returns True if
    the library successfully clears the challenge."""
    if not _hcaptcha_available():
        return False
    try:
        from hcaptcha_challenger.agents.playwright.control import AgentT  # type: ignore
    except Exception as exc:
        _log(f"hcaptcha-challenger import failed: {exc}")
        return False
    try:
        agent = AgentT.from_page(page=page, tmp_dir=Path("./out/hc_tmp"))
        # The challenger walks the iframe DOM, downloads challenge
        # images, classifies them with ONNX, and clicks. It returns a
        # status enum we treat as truthy on success.
        await agent.handle_checkbox()
        result = await agent.execute()
        _log(f"hcaptcha-challenger result: {result}")
        return bool(result and "success" in str(result).lower())
    except Exception as exc:
        _log(f"hcaptcha-challenger crashed: {exc}")
        return False


async def _wait_for_human_to_solve(page: Page) -> None:
    """When auto-solve isn't an option we just pause and let the user
    click through the challenge manually. Press Enter in the terminal
    once the box is green."""
    print("\n" + "=" * 60)
    print("hCaptcha detected — solve it in the browser window.")
    print("Once you're past it and you see the password field again,")
    print("come back here and press Enter to continue.")
    print("=" * 60 + "\n")
    # Block until the user presses Enter; we run input() in a thread
    # so the asyncio loop keeps servicing network events.
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, input, "")


# ── login flow ───────────────────────────────────────────────


async def _click_first_present(page: Page, selectors: List[str], *, timeout: float = 2000) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                await loc.click(timeout=timeout)
                _log(f"clicked: {sel}")
                return True
        except Exception:
            continue
    return False


async def _accept_cookies(page: Page) -> None:
    await _click_first_present(
        page,
        [
            "#onetrust-accept-btn-handler",
            "button:has-text('Accept all')",
            "button:has-text('Accept All')",
            "button:has-text('I accept')",
            "button:has-text('Tout accepter')",
        ],
        timeout=2000,
    )


async def _open_login_form(page: Page) -> None:
    """Click the entry point that opens the PingFederate sign-in
    page. The Accor top nav has a 'Sign in' button on desktop and a
    drawer on mobile; we try every reasonable selector."""
    candidates = [
        'button:has-text("Sign in")',
        'a:has-text("Sign in")',
        'button:has-text("Connexion")',
        '[data-tracking="login"]',
        '[data-tracking="signin"]',
        '[aria-label*="Sign in" i]',
        '[aria-label*="Connexion" i]',
        'button.loyalty__login',
        'a[href*="login.accor.com"]',
    ]
    if await _click_first_present(page, candidates):
        return
    # Fall back to a direct nav — the customer-API permalink kicks the
    # OAuth handshake.
    _log("no Sign-in button found; falling back to /account/en/my-bookings")
    await page.goto(ACCOR_ACCOUNT, wait_until="domcontentloaded")


async def _fill_credentials(page: Page, email: str, password: str) -> None:
    """Type the email + password into the PingFederate form. The page
    sometimes splits username + password across two screens, so we
    handle both layouts."""
    # Wait for the form to mount.
    await page.wait_for_selector(
        "input[type=email], input[name=pf.username], input#email, input[name=username]",
        timeout=30000,
    )
    for sel in (
        "input[name=pf.username]",
        "input[type=email]",
        "input#email",
        "input[name=username]",
    ):
        loc = page.locator(sel).first
        if await loc.count() > 0:
            await loc.fill(email)
            _log(f"filled email into {sel}")
            break

    # Some flows reveal the password field only after a 'Continue'
    # click on the username step.
    cont_buttons = [
        "button:has-text('Continue')",
        "button:has-text('Continuer')",
        "button#continue",
        "button[type=submit]",
    ]
    pw_visible = await page.locator(
        "input[name=pf.pass], input[type=password], input#password",
    ).first.count()
    if not pw_visible:
        await _click_first_present(page, cont_buttons, timeout=2500)
        with suppress(Exception):
            await page.wait_for_selector(
                "input[name=pf.pass], input[type=password], input#password",
                timeout=15000,
            )

    for sel in (
        "input[name=pf.pass]",
        "input[type=password]",
        "input#password",
    ):
        loc = page.locator(sel).first
        if await loc.count() > 0:
            await loc.fill(password)
            _log(f"filled password into {sel}")
            break


async def _submit_login(page: Page) -> None:
    """Click the final 'Sign in' button on the PingFederate form."""
    candidates = [
        "button:has-text('Sign in')",
        "button:has-text('Connexion')",
        "button:has-text('Log in')",
        "button:has-text('Continue')",
        "button[name=pf.ok]",
        "button[type=submit]",
        "input[type=submit]",
    ]
    if not await _click_first_present(page, candidates, timeout=3000):
        _log("WARN: couldn't find a submit button; pressing Enter as fallback")
        await page.keyboard.press("Enter")


async def _maybe_solve_hcaptcha(page: Page) -> None:
    """If hCaptcha is rendered on the current page, try auto-solve
    first then fall back to a human prompt."""
    frame = await _find_hcaptcha_frame(page)
    if not frame:
        return
    _log("hCaptcha frame detected")
    if await _try_solve_hcaptcha(page):
        _log("hCaptcha auto-solved")
        return
    if _hcaptcha_available():
        _log("hcaptcha-challenger failed; falling back to manual solve")
    else:
        _log("hcaptcha-challenger not installed (pip install hcaptcha-challenger to enable)")
    await _wait_for_human_to_solve(page)


# ── post-login crawl ─────────────────────────────────────────


ACCOUNT_TABS: List[tuple[str, str]] = [
    # (label, URL relative to all.accor.com)
    ("overview", "https://all.accor.com/account/en/my-bookings"),
    ("loyalty_points", "https://all.accor.com/account/en/my-rewards"),
    ("personal_data", "https://all.accor.com/account/en/my-personal-information"),
    ("preferences", "https://all.accor.com/account/en/my-preferences"),
    ("payment_methods", "https://all.accor.com/account/en/my-payment-methods"),
    ("communications", "https://all.accor.com/account/en/my-communications"),
]


async def _crawl_account(page: Page) -> Dict[str, Any]:
    """Visit each tab in the account SPA and wait long enough for the
    network responses to land. The actual capture happens in the
    response listener wired in main()."""
    summary: Dict[str, Any] = {}
    for label, url in ACCOUNT_TABS:
        _log(f"crawl: {label}  →  {url}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except PWTimeout:
            _log(f"  timeout loading {label}; continuing")
            continue
        # Account SPA fetches happen on idle, not on DOMContentLoaded.
        with suppress(PWTimeout):
            await page.wait_for_load_state("networkidle", timeout=20000)
        # Give a tiny extra beat for any deferred XHRs.
        await page.wait_for_timeout(1500)

        # Pull whatever the SPA has rendered into a simple snapshot.
        try:
            snap = await page.evaluate(
                """() => ({
                    title: document.title,
                    url: location.href,
                    points: document.querySelector('[data-test-id*="points"], [class*="loyalty-points"], [class*="rewards-points"]')?.innerText || null,
                    tier:   document.querySelector('[data-test-id*="card-level"], [class*="card-level"], [class*="tier"]')?.innerText || null,
                })"""
            )
        except Exception as exc:
            snap = {"error": str(exc)}
        summary[label] = snap
    return summary


async def _grab_page_state(page: Page) -> Dict[str, Any]:
    """Pull the Vuex store / Vue instance data out of the running
    SPA. Accor's account-vue2 mounts a Vuex store on `window.__store__`
    in several builds, and the customer object is exposed under
    `window.Modules.Accor.CustomerAPI.lastContact` once it's been
    fetched. Best-effort — wrapped in try/catch so the script doesn't
    fall over when the keys rename."""
    return await page.evaluate(
        """async () => {
            const out = {};
            try {
                out.customerApi = window.Modules?.Accor?.CustomerAPI ? Object.keys(window.Modules.Accor.CustomerAPI) : null;
                if (window.Modules?.Accor?.CustomerAPI?.getContact) {
                    try { out.contact = await window.Modules.Accor.CustomerAPI.getContact()(); } catch (e) { out.contact_err = String(e); }
                }
                if (window.Modules?.Accor?.CustomerAPI?.getToken) {
                    try { out.token = await window.Modules.Accor.CustomerAPI.getToken(); } catch (e) { out.token_err = String(e); }
                }
            } catch (e) { out.modules_err = String(e); }
            try {
                if (window.__store__) {
                    out.vuex_state = JSON.parse(JSON.stringify(window.__store__.state || {}));
                }
            } catch (e) { out.vuex_err = String(e); }
            return out;
        }"""
    )


# ── main ────────────────────────────────────────────────────


async def run(args: argparse.Namespace) -> int:
    email = args.email or os.environ.get("ACCOR_EMAIL") or ""
    password = args.password or os.environ.get("ACCOR_PASSWORD") or ""
    if not email or not password:
        print("ERROR: provide --email / --password or set ACCOR_EMAIL / ACCOR_PASSWORD",
              file=sys.stderr)
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    bag = APIBag()
    raw_log = OUT_DIR / f"accor_raw_{int(time.time())}.jsonl"

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=args.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-default-browser-check",
                "--disable-features=PrivacySandboxSettings4",
            ],
            proxy={"server": args.proxy} if args.proxy else None,
        )
        ctx = await browser.new_context(
            viewport={"width": 1366, "height": 850},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="Europe/Paris",
        )
        await ctx.add_init_script(_STEALTH_JS)
        await _wire_response_capture(ctx, bag, raw_log)

        page = await ctx.new_page()

        _log(f"navigating to {ACCOR_HOME}")
        await page.goto(ACCOR_HOME, wait_until="domcontentloaded", timeout=60000)
        await _accept_cookies(page)
        await page.wait_for_timeout(1500)

        _log("opening login form")
        await _open_login_form(page)
        # Wait for redirect over to login.accor.com.
        with suppress(PWTimeout):
            await page.wait_for_url(re.compile(r"login\.accor\.com|api\.accor\.com/authentication"),
                                    timeout=30000)
        await page.wait_for_load_state("domcontentloaded")

        _log("filling credentials")
        await _fill_credentials(page, email, password)

        _log("submitting form")
        await _submit_login(page)

        # hCaptcha can appear after submit. Loop a couple of times in
        # case it re-renders.
        for _ in range(3):
            await page.wait_for_timeout(2500)
            await _maybe_solve_hcaptcha(page)
            # If we've already left the login host we're in.
            if "login.accor.com" not in (page.url or ""):
                break
            # Re-submit if we're still on the login form (the captcha
            # solver clears the box; some flows still need a 2nd
            # click).
            with suppress(Exception):
                await _submit_login(page)

        # Confirm login by waiting until a *known* auth'd URL responds.
        _log("waiting for login to settle")
        login_ok = False
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            # Sniff the captured-bag for any 2xx body from the
            # logged-in URLs.
            for item in bag.items.values():
                if any(s in item.url for s in _LOGIN_OK_URLS) and 200 <= item.status < 300:
                    login_ok = True
                    break
            if login_ok:
                break
            await asyncio.sleep(1.5)

        if not login_ok:
            # Save evidence so the user can see what went wrong.
            err_html = OUT_DIR / "login_failed.html"
            err_png = OUT_DIR / "login_failed.png"
            with suppress(Exception):
                err_html.write_text(await page.content())
            with suppress(Exception):
                await page.screenshot(path=str(err_png), full_page=True)
            _log(f"login could not be verified — see {err_html} + {err_png}")
            if not args.keep_open:
                await browser.close()
            return 3

        _log("login OK")
        # Crawl every tab in the account SPA.
        summary = await _crawl_account(page)
        # Pull anything sitting on window.* / Vuex.
        spa_state = await _grab_page_state(page)

        # Identify the user so the output filename is useful.
        member_id = "unknown"
        for item in bag.items.values():
            body = item.body if isinstance(item.body, dict) else None
            if not body:
                continue
            for key in ("memberId", "cardNumber", "customerId", "id"):
                val = body.get(key) if isinstance(body, dict) else None
                if val and str(val) != "0":
                    member_id = str(val)
                    break
            if member_id != "unknown":
                break

        cookies = await ctx.cookies()
        storage_state = await ctx.storage_state()

        dump = {
            "captured_at": _now(),
            "member_id": member_id,
            "spa_state": spa_state,
            "tab_summary": summary,
            "captured_apis": bag.to_list(),
            "cookies": cookies,
            "storage_state": storage_state,
        }

        out_path = OUT_DIR / f"accor_account_{_slugify(member_id)}.json"
        _safe_write(out_path, dump)
        _log(f"wrote full dump to {out_path}")
        _log(f"raw response log:  {raw_log}")

        if args.keep_open:
            print("\nBrowser left open (--keep-open). Press Ctrl+C in the terminal when done.\n")
            try:
                while True:
                    await asyncio.sleep(3600)
            except KeyboardInterrupt:
                pass

        await browser.close()
        return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--email", default=None)
    p.add_argument("--password", default=None)
    p.add_argument("--proxy", default=None,
                   help="Proxy URL, e.g. http://user:pass@host:port")
    headed = p.add_mutually_exclusive_group()
    headed.add_argument("--headed", dest="headless", action="store_false", default=False)
    headed.add_argument("--headless", dest="headless", action="store_true")
    p.add_argument("--keep-open", action="store_true",
                   help="Don't close the browser when the dump finishes.")
    return p.parse_args()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(run(_parse_args())))
    except KeyboardInterrupt:
        sys.exit(130)

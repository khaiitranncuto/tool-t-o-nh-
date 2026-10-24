"""Close tool-owned Flow browsers when the parent tool process disappears."""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 0x00000102
INFINITE = 0xFFFFFFFF


def wait_for_process_exit(pid: int) -> None:
    """Wait on one process handle so PID reuse cannot change ownership."""
    if pid <= 0:
        return
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return
    try:
        kernel32.WaitForSingleObject(handle, INFINITE)
    finally:
        kernel32.CloseHandle(handle)


def owns_instance(owner_path: Path, token: str) -> bool:
    """Only the newest server instance may authorize browser cleanup."""
    try:
        payload = json.loads(owner_path.read_text(encoding="utf-8"))
        return str(payload.get("token", "")) == token
    except (OSError, json.JSONDecodeError):
        return False


def read_accounts(config_path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        accounts = payload.get("accounts", [])
        return accounts if isinstance(accounts, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def close_exact_profile_gracefully(account: dict[str, Any]) -> None:
    """Ask only the root Chromium window for this exact profile to close."""
    profile = str(account.get("helper_profile_dir", "") or account.get("profile_dir", "")).strip()
    if not profile or os.name != "nt":
        return
    environment = os.environ.copy()
    environment["FLOW_WATCHDOG_PROFILE"] = profile
    command = (
        "$profile=$env:FLOW_WATCHDOG_PROFILE; "
        "$items=Get-CimInstance Win32_Process | Where-Object {"
        "$_.CommandLine -and $_.CommandLine.IndexOf($profile,[StringComparison]::OrdinalIgnoreCase) -ge 0 "
        "-and $_.CommandLine -notmatch '--type=' -and $_.CommandLine -match '--remote-debugging-port='}; "
        "foreach($item in $items){"
        "$p=Get-Process -Id $item.ProcessId -ErrorAction SilentlyContinue; "
        "if($p){$null=$p.CloseMainWindow()}"
        "}"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=12,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except Exception:
        pass


def force_close_exact_profile(account: dict[str, Any]) -> None:
    """Kill only a root Chromium process containing this exact profile path."""
    profile = str(account.get("helper_profile_dir", "") or account.get("profile_dir", "")).strip()
    if not profile:
        return
    environment = os.environ.copy()
    environment["FLOW_WATCHDOG_PROFILE"] = profile
    command = (
        "$profile=$env:FLOW_WATCHDOG_PROFILE; "
        "$items=Get-CimInstance Win32_Process | Where-Object {"
        "$_.CommandLine -and $_.CommandLine.IndexOf($profile,[StringComparison]::OrdinalIgnoreCase) -ge 0 "
        "-and $_.CommandLine -notmatch '--type=' -and $_.CommandLine -match '--remote-debugging-port='}; "
        "foreach($item in $items){Stop-Process -Id $item.ProcessId -Force -ErrorAction SilentlyContinue}"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=12,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except Exception:
        pass


def cleanup(config_path: Path, owner_path: Path, token: str) -> None:
    if not owns_instance(owner_path, token):
        return
    accounts = read_accounts(config_path)
    for account in accounts:
        if not owns_instance(owner_path, token):
            return
        close_exact_profile_gracefully(account)
    deadline = time.time() + 6
    while time.time() < deadline:
        live_ports = []
        for account in accounts:
            if not owns_instance(owner_path, token):
                return
            port = int(account.get("helper_port", 0) or account.get("port", 0) or 0)
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=0.3).close()
                live_ports.append(port)
            except Exception:
                pass
        if not live_ports:
            return
        time.sleep(0.4)
    for account in accounts:
        if not owns_instance(owner_path, token):
            return
        force_close_exact_profile(account)


def main() -> int:
    if os.name != "nt" or len(sys.argv) != 5:
        return 2
    try:
        parent_pid = int(sys.argv[1])
    except ValueError:
        return 2
    config_path = Path(sys.argv[2])
    owner_path = Path(sys.argv[3])
    token = sys.argv[4]
    wait_for_process_exit(parent_pid)
    cleanup(config_path, owner_path, token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

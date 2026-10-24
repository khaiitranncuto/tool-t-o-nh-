"""Cursor Run/F5 entry point with single-instance cleanup."""
from pathlib import Path
import subprocess
import sys


root = Path(__file__).resolve().parent
result = subprocess.run(
    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(root / "run.ps1"), "-CleanupOnly"],
    cwd=root,
)
if result.returncode:
    sys.exit(result.returncode)

import app as tool

tool.DATA_DIR.mkdir(parents=True, exist_ok=True)
tool.RESULT_DIR.mkdir(parents=True, exist_ok=True)
tool.AUDIO_RESULT_DIR.mkdir(parents=True, exist_ok=True)
tool.restore_last_project()
tool.add_log("Python Web UI đã khởi động tại http://127.0.0.1:5050")
tool.start_browser_watchdog()
try:
    tool.app.run(host="127.0.0.1", port=5050, debug=False, threaded=True)
finally:
    tool.close_all_managed_browsers()

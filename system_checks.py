"""Background system and tool health checks for phantom-rail.

Implements startup and on-demand verification for:
- ffmpeg & ffprobe binaries
- FFmpeg encoders (aac, mjpeg)
- JavaScript runtime (deno / node / nodejs) & yt-dlp-ejs
- yt-dlp, yt-dlp-ejs & certifi auto-update with automatic nightly fallback
- Download folder write permissions & free disk space check
- Metadata API reachability (api.audible.de & api.audnex.us)
"""

import importlib
import importlib.metadata
import json
import queue
import os
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from flask import jsonify, request

try:
    import certifi
except ImportError:
    certifi = None


MIN_FREE_DISK_GB = float(os.environ.get("MIN_FREE_DISK_GB", "1.5"))
YTDLP_CHANNEL = os.environ.get("YTDLP_CHANNEL", "stable").strip().lower()


def _run_cmd(cmd, timeout=15):
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if res.returncode == 0:
            return (res.stdout or "").strip()
    except Exception:
        pass
    return ""


def _extract_tool_version(binary, pattern):
    out = _run_cmd([binary, "-version"])
    if not out:
        return "fehlt!"
    match = re.search(pattern, out)
    return match.group(1) if match else "installiert"

# ... remaining health-check module source ...

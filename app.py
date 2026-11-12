from flask import Flask, render_template, request, Response, jsonify
import subprocess
import os
import json
import tempfile
import glob
import urllib.request
import urllib.parse
import re
import html
import shutil
import threading
import queue
import time
import uuid
import sys
import importlib.metadata
import importlib.util
import ssl
from folder_support import register_folders
from system_checks import register_system_checks

app = Flask(__name__)
DOWNLOAD_FOLDER = os.environ.get("DOWNLOAD_FOLDER", "/downloads")
TMP_BASE_DIR = os.path.join(DOWNLOAD_FOLDER, ".tmp_work")

os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(TMP_BASE_DIR, exist_ok=True)

resolve_destination = register_folders(app, DOWNLOAD_FOLDER)

# Alte temporaere Reste beim Start bereinigen
try:
    for old_item in glob.glob(os.path.join(TMP_BASE_DIR, "work_*")):
        if os.path.isdir(old_item):
            shutil.rmtree(old_item, ignore_errors=True)
except Exception:
    pass

SYSTEM_STATUS = {
    "state": "checking",
    "message": "Initialisiere Systemchecks...",
    "progress": 0,
    "progress_step": "Initialisiere Systemchecks",
    "yt_dlp_version": "",
    "ffmpeg_version": "",
    "ffprobe_version": "",
    "checks": {},
    "metadata_apis": {"api.audible.de": False, "api.audnex.us": False},
    "updated": False,
    "update_mode": "",
    "last_checked": None,
}
SYSTEM_STATUS_LOCK = threading.Lock()

CURRENT_JOB = {
    "id": None,
    "status": "idle",
    "message": "Bereit",
    "dl_progress": 0,
    "proc_progress": 0,
    "progress": 0,
    "events": [],
    "updated_at": time.time(),
}
CURRENT_JOB_LOCK = threading.Lock()


def _update_job(payload):
    with CURRENT_JOB_LOCK:
        CURRENT_JOB.update(payload)
        CURRENT_JOB["updated_at"] = time.time()
        event = {"type": "update", **payload, "updated_at": CURRENT_JOB["updated_at"]}
        CURRENT_JOB["events"].append(event)
        if len(CURRENT_JOB["events"]) > 200:
            CURRENT_JOB["events"] = CURRENT_JOB["events"][-200:]


# Startup check and /system_status route are owned by system_checks.py.
register_system_checks(app, SYSTEM_STATUS, SYSTEM_STATUS_LOCK, DOWNLOAD_FOLDER, CURRENT_JOB)

# ... remaining application source ...

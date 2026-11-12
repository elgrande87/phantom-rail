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


def _check_ffmpeg_encoders():
    out = _run_cmd(["ffmpeg", "-hide_banner", "-encoders"])
    if not out:
        return {"aac": False, "mjpeg": False, "ok": False}
    has_aac = bool(re.search(r"^\s*A[.\S]{5}\s+aac\b", out, re.MULTILINE))
    has_mjpeg = bool(re.search(r"^\s*V[.\S]{5}\s+mjpeg\b", out, re.MULTILINE))
    return {"aac": has_aac, "mjpeg": has_mjpeg, "ok": has_aac and has_mjpeg}


def _check_js_runtime():
    for candidate, args in (
        ("deno", ["deno", "--version"]),
        ("node", ["node", "--version"]),
        ("nodejs", ["nodejs", "--version"]),
    ):
        if shutil.which(candidate):
            out = _run_cmd(args, timeout=8)
            first_line = out.splitlines()[0].strip() if out else "installiert"
            return {"available": True, "runtime": candidate, "version": first_line}
    return {"available": False, "runtime": None, "version": "fehlt!"}


def _check_ejs_installed():
    try:
        ver = importlib.metadata.version("yt-dlp-ejs")
        return {"installed": True, "version": ver}
    except importlib.metadata.PackageNotFoundError:
        return {"installed": False, "version": "fehlt!"}
    except Exception:
        return {"installed": False, "version": "unbekannt"}


def _reload_certifi():
    global certifi
    try:
        if certifi is not None:
            certifi = importlib.reload(certifi)
        else:
            import certifi as _certifi
            certifi = _certifi
        ca_path = certifi.where()
        if ca_path and os.path.isfile(ca_path):
            os.environ["SSL_CERT_FILE"] = ca_path
            os.environ["REQUESTS_CA_BUNDLE"] = ca_path
        try:
            ver = importlib.metadata.version("certifi")
        except Exception:
            ver = getattr(certifi, "__version__", "installiert")
        return {"ok": True, "version": ver, "ca_bundle": ca_path}
    except Exception as exc:
        return {"ok": False, "version": "Fehler", "error": str(exc)}


def _update_packages_with_nightly_fallback(preferred_channel="stable"):
    """Update yt-dlp[default], yt-dlp-ejs and certifi via pip, falling back to nightly (--pre)."""
    python_exe = sys.executable or "python3"
    base_pkgs = ["yt-dlp[default]", "yt-dlp-ejs", "certifi"]

    def _pip_install(pre=False):
        cmd = [python_exe, "-m", "pip", "install", "--no-cache-dir", "-q", "-U"]
        if pre:
            cmd.append("--pre")
        cmd.extend(base_pkgs)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=180)

    used_channel = "nightly" if preferred_channel == "nightly" else "stable"
    fallback_triggered = False

    try:
        res = _pip_install(pre=(used_channel == "nightly"))
        after_ver = _run_cmd(["yt-dlp", "--version"])
        if res.returncode != 0 or not after_ver:
            if used_channel != "nightly":
                fallback_triggered = True
                used_channel = "nightly"
                _pip_install(pre=True)
    except Exception:
        if used_channel != "nightly":
            try:
                fallback_triggered = True
                used_channel = "nightly"
                _pip_install(pre=True)
            except Exception as fallback_err:
                print(f"Nightly-Fallback fehlgeschlagen: {fallback_err}")

    return {"channel": used_channel, "nightly_fallback": fallback_triggered}


def check_storage(download_folder):
    """Check write permissions and free disk space in download_folder."""
    writable = False
    write_error = ""
    test_file = os.path.join(download_folder, f".write_test_{os.getpid()}")
    try:
        os.makedirs(download_folder, exist_ok=True)
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(test_file)
        writable = True
    except Exception as exc:
        write_error = str(exc)
    finally:
        if os.path.exists(test_file):
            try:
                os.remove(test_file)
            except Exception:
                pass

    free_gb = 0.0
    total_gb = 0.0
    disk_ok = False
    try:
        usage = shutil.disk_usage(download_folder)
        free_gb = round(usage.free / (1024 ** 3), 2)
        total_gb = round(usage.total / (1024 ** 3), 2)
        disk_ok = free_gb >= MIN_FREE_DISK_GB
    except Exception as exc:
        if not write_error:
            write_error = str(exc)

    return {
        "writable": writable,
        "free_gb": free_gb,
        "total_gb": total_gb,
        "min_free_gb": MIN_FREE_DISK_GB,
        "disk_ok": disk_ok,
        "ok": writable and disk_ok,
        "error": write_error,
    }


def _check_metadata_apis():
    ssl_ctx = None
    try:
        if certifi is not None:
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ssl_ctx = None

    endpoints = {
        "audible_de": "https://api.audible.de/1.0/catalog/products?num_results=1&keywords=test",
        "audnex_us": "https://api.audnex.us/books/B000000000",
    }
    results = {}
    headers = {"User-Agent": "Mozilla/5.0 (phantom-rail-healthcheck)"}

    for key, url in endpoints.items():
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=4, context=ssl_ctx) as resp:
                code = getattr(resp, "status", 200)
                results[key] = {"reachable": 200 <= code < 500, "status": code}
        except urllib.error.HTTPError as http_err:
            # HTTP 400/404 from Audnex for dummy ASIN still proves DNS/TLS/API reachability
            reachable = 200 <= http_err.code < 500
            results[key] = {"reachable": reachable, "status": http_err.code}
        except Exception as exc:
            results[key] = {"reachable": False, "error": str(exc)}

    all_ok = all(item.get("reachable", False) for item in results.values())
    return {"endpoints": results, "ok": all_ok}


def register_system_checks(app, system_status, system_status_lock, download_folder, current_job=None):
    """Replace the /system_status view function and run the enhanced startup check."""

    def run_enhanced_tool_check(force=False):
        with system_status_lock:
            if system_status.get("state") in ("checking", "updating") and not force:
                return
            system_status["state"] = "checking"
            system_status["message"] = "Prüfe ffmpeg, ffprobe, Encoder & JS-Runtime..."

        ff_ver = _extract_tool_version("ffmpeg", r"ffmpeg version\s+([^\s]+)")
        fp_ver = _extract_tool_version("ffprobe", r"ffprobe version\s+([^\s]+)")
        encoders = _check_ffmpeg_encoders()
        js_info = _check_js_runtime()
        before_ver = _run_cmd(["yt-dlp", "--version"])

        with system_status_lock:
            system_status["state"] = "updating"
            system_status["message"] = "Aktualisiere yt-dlp, EJS & certifi..."
            system_status["yt_dlp_version"] = before_ver
            system_status["ffmpeg_version"] = ff_ver
            system_status["ffprobe_version"] = fp_ver
            system_status["encoders"] = encoders
            system_status["js_runtime"] = js_info

        # Avoid pip-updating while an active download job is running
        job_active = bool(current_job and current_job.get("active"))
        update_info = {"channel": YTDLP_CHANNEL, "nightly_fallback": False, "skipped_due_to_active_job": job_active}
        if not job_active:
            update_info.update(_update_packages_with_nightly_fallback(YTDLP_CHANNEL))

        cert_info = _reload_certifi()
        ejs_info = _check_ejs_installed()
        after_ver = _run_cmd(["yt-dlp", "--version"]) or before_ver

        with system_status_lock:
            system_status["message"] = "Prüfe Speicherplatz, Schreibrechte & Metadaten-APIs..."

        storage_info = check_storage(download_folder)
        api_info = _check_metadata_apis()

        warnings = []
        if not after_ver:
            warnings.append("yt-dlp fehlt")
        if ff_ver == "fehlt!":
            warnings.append("ffmpeg fehlt")
        if fp_ver == "fehlt!":
            warnings.append("ffprobe fehlt")
        if not encoders.get("ok"):
            missing_enc = [k for k in ("aac", "mjpeg") if not encoders.get(k)]
            warnings.append(f"FFmpeg-Encoder fehlt ({', '.join(missing_enc)})")
        if not js_info.get("available"):
            warnings.append("JS-Runtime (node/deno) fehlt")
        if not ejs_info.get("installed"):
            warnings.append("yt-dlp-ejs fehlt")
        if not cert_info.get("ok"):
            warnings.append("certifi Fehler")
        if not storage_info.get("writable"):
            warnings.append("Keine Schreibrechte in /downloads")
        elif not storage_info.get("disk_ok"):
            warnings.append(f"Wenig Speicher ({storage_info.get('free_gb')} GB frei)")
        if not api_info.get("ok"):
            down_apis = [k for k, v in api_info["endpoints"].items() if not v.get("reachable")]
            warnings.append(f"API nicht erreichbar ({', '.join(down_apis)})")

        channel_suffix = " [nightly]" if update_info.get("nightly_fallback") or update_info.get("channel") == "nightly" else ""

        with system_status_lock:
            system_status["yt_dlp_version"] = after_ver or "unbekannt"
            system_status["yt_dlp_channel"] = update_info.get("channel", "stable")
            system_status["nightly_fallback"] = bool(update_info.get("nightly_fallback"))
            system_status["ffmpeg_version"] = ff_ver
            system_status["ffprobe_version"] = fp_ver
            system_status["encoders"] = encoders
            system_status["js_runtime"] = js_info
            system_status["yt_dlp_ejs"] = ejs_info
            system_status["certifi"] = cert_info
            system_status["storage"] = storage_info
            system_status["metadata_apis"] = api_info
            system_status["warnings"] = warnings
            system_status["last_checked"] = int(time.time())
            system_status["updated"] = bool(before_ver and after_ver and before_ver != after_ver)

            if not warnings:
                system_status["state"] = "up_to_date"
                system_status["message"] = (
                    f"yt-dlp v{after_ver}{channel_suffix} | "
                    f"{storage_info.get('free_gb')} GB frei | Alle Checks OK"
                )
            else:
                system_status["state"] = "warning"
                system_status["message"] = "Warnung: " + "; ".join(warnings)

    def enhanced_system_status_route():
        if request.method == "POST":
            threading.Thread(target=run_enhanced_tool_check, kwargs={"force": True}, daemon=True).start()
        with system_status_lock:
            return jsonify(dict(system_status))

    if "system_status_route" in app.view_functions:
        app.view_functions["system_status_route"] = enhanced_system_status_route
    else:
        app.add_url_rule("/system_status", "system_status_route", enhanced_system_status_route, methods=["GET", "POST"])

    threading.Thread(target=run_enhanced_tool_check, kwargs={"force": True}, daemon=True).start()
    return run_enhanced_tool_check

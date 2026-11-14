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


def _latest_stable_yt_dlp_version(timeout=3):
    """Read PyPI's canonical stable version; None means the lookup was inconclusive."""
    req = urllib.request.Request("https://pypi.org/pypi/yt-dlp/json",
                                 headers={"User-Agent": "phantom-rail-healthcheck"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return str(payload["info"]["version"])
    except Exception:
        return None


def _should_update_yt_dlp(installed_version, latest_version):
    """Avoid needless pip upgrade work, comparing PEP 440 versions canonically."""
    if not installed_version or not latest_version:
        return True
    try:
        from packaging.version import Version
        return Version(str(installed_version).strip()) < Version(str(latest_version).strip())
    except Exception:
        return not _same_version(installed_version, latest_version)


def _update_packages_with_nightly_fallback(preferred_channel="stable", progress_callback=None):
    """Update packages, reporting a coarse (time/stage based) pip progress estimate."""
    python_exe = sys.executable or "python3"
    base_pkgs = ["yt-dlp[default]", "yt-dlp-ejs", "certifi"]

    def _pip_install(pre=False, progress_start=25, progress_end=66):
        cmd = [python_exe, "-m", "pip", "install", "--no-cache-dir", "--progress-bar", "off", "-U"]
        if pre:
            cmd.append("--pre")
        cmd.extend(base_pkgs)
        label = "nightly" if pre else "stable"
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        lines = queue.Queue()

        def read_output():
            try:
                for line in proc.stdout:
                    lines.put(line.strip())
            finally:
                lines.put(None)

        threading.Thread(target=read_output, daemon=True).start()
        started = time.monotonic()
        best = progress_start
        output_done = False
        last_detail = f"pip {label}: starte"
        stage_points = (("collecting", 0.10), ("downloading", 0.27),
                        ("installing collected packages", 0.55), ("successfully installed", 0.90),
                        ("requirement already satisfied", 0.90))
        while proc.poll() is None:
            try:
                line = lines.get(timeout=0.5)
                if line is None:
                    output_done = True
                elif line:
                    lowered = line.lower()
                    last_detail = f"pip {label}: {line[:120]}"
                    for marker, fraction in stage_points:
                        if marker in lowered:
                            best = max(best, progress_start + int((progress_end-progress_start)*fraction))
                            break
            except queue.Empty:
                pass

            # pip's output does not expose a reliable aggregate byte percentage. This
            # bounded elapsed-time estimate keeps the badge moving but is labeled as such.
            elapsed = time.monotonic() - started
            estimate = progress_start + int((progress_end-progress_start)*0.82*min(elapsed/120.0, 1.0))
            best = max(best, estimate)
            if progress_callback:
                detail = last_detail if output_done is False else f"pip {label}: Abschluss läuft"
                progress_callback(best, f"{detail} · ca. {int(elapsed)} s (Schätzwert)")
            if elapsed >= 180:
                proc.kill()
                proc.wait()
                raise subprocess.TimeoutExpired(cmd, 180)
        proc.wait()
        if progress_callback:
            progress_callback(progress_end, f"pip {label}: beendet" if proc.returncode == 0 else f"pip {label}: Fehler")
        return subprocess.CompletedProcess(cmd, proc.returncode, "", "")

    used_channel = "nightly" if preferred_channel == "nightly" else "stable"
    fallback_triggered = False
    try:
        if used_channel == "nightly":
            res = _pip_install(pre=True, progress_start=25, progress_end=68)
        else:
            res = _pip_install(pre=False, progress_start=25, progress_end=60)
    except Exception as exc:
        res = subprocess.CompletedProcess([], 1, "", str(exc))
    after_ver = _run_cmd(["yt-dlp", "--version"])
    if res.returncode != 0 or not after_ver:
        if used_channel != "nightly":
            fallback_triggered = True
            used_channel = "nightly"
            try:
                res = _pip_install(pre=True, progress_start=60, progress_end=70)
            except Exception as exc:
                res = subprocess.CompletedProcess([], 1, "", str(exc))
    return {"channel": used_channel, "nightly_fallback": fallback_triggered,
            "pip_returncode": res.returncode}


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



def _same_version(left, right):
    """Compare version labels semantically (e.g. 2026.03.03 == 2026.3.3)."""
    try:
        from packaging.version import Version
        return Version(str(left).strip()) == Version(str(right).strip())
    except Exception:
        def normalized(value):
            return tuple(int(part) for part in re.findall(r"\d+", str(value)))
        return normalized(left) == normalized(right)


def _check_metadata_apis_for_endpoint(key, url):
    ssl_ctx = None
    try:
        if certifi is not None:
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (phantom-rail-healthcheck)"})
    try:
        with urllib.request.urlopen(req, timeout=4, context=ssl_ctx) as resp:
            code = getattr(resp, "status", 200)
            return {"reachable": 200 <= code < 500, "status": code}
    except urllib.error.HTTPError as exc:
        return {"reachable": 200 <= exc.code < 500, "status": exc.code}
    except Exception as exc:
        return {"reachable": False, "error": str(exc)}

def register_system_checks(app, system_status, system_status_lock, download_folder, current_job=None):
    """Replace the /system_status view function and run the enhanced startup check."""

    def run_enhanced_tool_check(force=False):
        def set_progress(percent, message, state="checking"):
            with system_status_lock:
                system_status.update(state=state, progress=max(0, min(100, int(percent))),
                                     progress_step=message, message=message)

        with system_status_lock:
            if system_status.get("state") in ("checking", "updating") and not force:
                return
            system_status.update(state="checking", progress=0,
                                 progress_step="Initialisiere Systemchecks", message="Initialisiere Systemchecks")

        set_progress(3, "Prüfe ffmpeg")
        ff_ver = _extract_tool_version("ffmpeg", r"ffmpeg version\s+([^\s]+)")
        set_progress(9, "Prüfe ffprobe")
        fp_ver = _extract_tool_version("ffprobe", r"ffprobe version\s+([^\s]+)")
        set_progress(15, "Prüfe FFmpeg-Encoder")
        encoders = _check_ffmpeg_encoders()
        set_progress(20, "Prüfe JavaScript-Runtime")
        js_info = _check_js_runtime()
        set_progress(24, "Prüfe installierte yt-dlp-Version")
        before_ver = _run_cmd(["yt-dlp", "--version"])

        with system_status_lock:
            system_status.update(state="updating", progress=25, progress_step="Aktualisiere yt-dlp, EJS & certifi",
                                 message="Aktualisiere yt-dlp, EJS & certifi...", yt_dlp_version=before_ver,
                                 ffmpeg_version=ff_ver, ffprobe_version=fp_ver, encoders=encoders, js_runtime=js_info)

        job_active = bool(current_job and current_job.get("active"))
        latest_ver = _latest_stable_yt_dlp_version()
        update_info = {"channel": YTDLP_CHANNEL, "nightly_fallback": False,
                       "skipped_due_to_active_job": job_active,
                       "latest_stable_version": latest_ver}
        needs_update = YTDLP_CHANNEL == "nightly" or _should_update_yt_dlp(before_ver, latest_ver)
        if not job_active and needs_update:
            update_info.update(_update_packages_with_nightly_fallback(
                YTDLP_CHANNEL, progress_callback=lambda pct, msg: set_progress(pct, msg, "updating")))
        elif job_active:
            set_progress(70, "pip-Update wegen aktivem Download übersprungen", "checking")
        else:
            update_info["skipped_current"] = True
            set_progress(70, f"yt-dlp {before_ver} ist bereits aktuell (PyPI {latest_ver})", "checking")

        set_progress(73, "Lade certifi-Zertifikate neu")
        cert_info = _reload_certifi()
        set_progress(77, "Prüfe yt-dlp-ejs")
        ejs_info = _check_ejs_installed()
        after_ver = _run_cmd(["yt-dlp", "--version"]) or before_ver

        set_progress(81, "Prüfe Download-Speicher")
        storage_info = check_storage(download_folder)
        set_progress(88, "Prüfe Audible-Metadaten-API")
        api_results = {}
        for key, url in (("audible_de", "https://api.audible.de/1.0/catalog/products?num_results=1&keywords=test"),
                         ("audnex_us", "https://api.audnex.us/books/B000000000")):
            api_results[key] = _check_metadata_apis_for_endpoint(key, url)
            set_progress(88 if key == "audible_de" else 96,
                         "Prüfe Audnex-Metadaten-API" if key == "audible_de" else "Schließe Systemchecks ab")
        api_info = {"endpoints": api_results, "ok": all(x.get("reachable", False) for x in api_results.values())}

        warnings = []
        if not after_ver: warnings.append("yt-dlp fehlt")
        if ff_ver == "fehlt!": warnings.append("ffmpeg fehlt")
        if fp_ver == "fehlt!": warnings.append("ffprobe fehlt")
        if not encoders.get("ok"):
            missing_enc = [k for k in ("aac", "mjpeg") if not encoders.get(k)]
            warnings.append(f"FFmpeg-Encoder fehlt ({', '.join(missing_enc)})")
        if not js_info.get("available"): warnings.append("JS-Runtime (node/deno) fehlt")
        if not ejs_info.get("installed"): warnings.append("yt-dlp-ejs fehlt")
        if not cert_info.get("ok"): warnings.append("certifi Fehler")
        if not storage_info.get("writable"): warnings.append("Keine Schreibrechte im Download-Ordner")
        elif not storage_info.get("disk_ok"): warnings.append(f"Wenig Speicher ({storage_info.get('free_gb')} GB frei)")
        if not api_info.get("ok"):
            down_apis = [k for k, v in api_results.items() if not v.get("reachable")]
            warnings.append(f"API nicht erreichbar ({', '.join(down_apis)})")

        channel_suffix = " [nightly]" if update_info.get("nightly_fallback") or update_info.get("channel") == "nightly" else ""
        with system_status_lock:
            system_status.update(progress=100, progress_step="Systemchecks abgeschlossen",
                yt_dlp_version=after_ver or "unbekannt", yt_dlp_latest_version=latest_ver,
                yt_dlp_update_skipped=bool(update_info.get("skipped_current")),
                yt_dlp_channel=update_info.get("channel", "stable"),
                nightly_fallback=bool(update_info.get("nightly_fallback")), ffmpeg_version=ff_ver,
                ffprobe_version=fp_ver, encoders=encoders, js_runtime=js_info, yt_dlp_ejs=ejs_info,
                certifi=cert_info, storage=storage_info, metadata_apis=api_info, warnings=warnings,
                last_checked=int(time.time()), updated=bool(before_ver and after_ver and not _same_version(before_ver, after_ver)))
            system_status["state"] = "warning" if warnings else "up_to_date"
            system_status["message"] = ("Warnung: " + "; ".join(warnings)) if warnings else (
                f"yt-dlp v{after_ver}{channel_suffix} | {storage_info.get('free_gb')} GB frei | Alle Checks OK")

    def enhanced_system_status_route():
        if request.method == "POST":
            threading.Thread(target=run_enhanced_tool_check, kwargs={"force": True}, daemon=True).start()
        with system_status_lock:
            return jsonify(dict(system_status))

    if "system_status_route" in app.view_functions:
        app.view_functions["system_status_route"] = enhanced_system_status_route
    else:
        app.add_url_rule("/system_status", "system_status_route", enhanced_system_status_route, methods=["GET", "POST"])
    # API alias used by the percentage badge; the response includes `progress`.
    if "api_yt_dlp_status" not in app.view_functions:
        app.add_url_rule("/api/yt-dlp-status", "api_yt_dlp_status",
                         enhanced_system_status_route, methods=["GET", "POST"])

    threading.Thread(target=run_enhanced_tool_check, kwargs={"force": True}, daemon=True).start()
    return run_enhanced_tool_check

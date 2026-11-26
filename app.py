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
import signal
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
except Exception as e:
    print(f"Konnte alte Temp-Ordner nicht aufraeumen: {e}")

# === HINTERGRUND-JOB-MANAGER (ueberlebt Seiten-Refresh & iOS Standby) ===
JOB_LOCK = threading.Lock()
JOB_COND = threading.Condition(JOB_LOCK)
CURRENT_JOB = {
    "id": None,
    "active": False,
    "cancelled": False,
    "status": "idle",
    "title": "",
    "dl_progress": 0,
    "proc_progress": 0,
    "progress": 0,
    "dl_label": "1. Download-Fortschritt (Gesamtes Buch)",
    "events": [],
    "updated_at": 0.0,
    "work_dir": None,
    "current_subprocesses": [],
    "target_file": None,
}

def _emit_job_event(payload):
    with JOB_COND:
        st = payload.get("status")
        if st:
            CURRENT_JOB["status"] = st
        if "dl_progress" in payload:
            CURRENT_JOB["dl_progress"] = payload["dl_progress"]
        if "proc_progress" in payload:
            CURRENT_JOB["proc_progress"] = payload["proc_progress"]
        if "progress" in payload:
            CURRENT_JOB["progress"] = payload["progress"]
        msg_text = payload.get("msg") or payload.get("message") or ""
        if st == "downloading" and re.search(r"Kapitel \d+\s*/", msg_text):
            CURRENT_JOB["dl_label"] = msg_text.split("— Gesamt:")[0].strip()

        events = CURRENT_JOB["events"]
        if payload.get("replace_line") and events and events[-1].get("replace_line"):
            events[-1] = dict(payload)
        else:
            events.append(dict(payload))
            if len(events) > 400:
                del events[:100]

        if st in ("done", "error", "cancelled"):
            CURRENT_JOB["active"] = False
        CURRENT_JOB["updated_at"] = time.time()
        JOB_COND.notify_all()

# === AUTO-UPDATER & TOOL-STATUS BEIM START ===
SYSTEM_STATUS_LOCK = threading.Lock()
SYSTEM_STATUS = {
    "state": "checking",
    "message": "Prüfe yt-dlp & Systemwerkzeuge...",
    "yt_dlp_version": "",
    "ffmpeg_version": "",
    "updated": False,
    "last_checked": 0,
    "ffprobe_version": "nicht geprüft",
    "javascript_runtime": "nicht geprüft",
    "yt_dlp_ejs": False,
    "ffmpeg_encoders": {"aac": False, "mjpeg": False},
    "download_storage": {"writable": False, "free_bytes": 0, "total_bytes": 0},
    "metadata_apis": {"api.audible.de": False, "api.audnex.us": False},
    "checks": {},
}

register_system_checks(app, SYSTEM_STATUS, SYSTEM_STATUS_LOCK, DOWNLOAD_FOLDER, CURRENT_JOB)


@app.route('/')
def index():
    return render_template('index.html')


def clean_html(raw_html):
    if not raw_html:
        return ''
    return html.unescape(re.sub(r'<[^>]+>', '', str(raw_html))).strip()

def clean_search_query(query):
    text = html.unescape(str(query or '')).strip()
    prefix = re.compile(
        r'^(?:intro|outro|credits?|kapitel\s*\d+|track\s*\d+|'
        r'teil\s*\d+|part\s*\d+)\s*:\s*', re.I)
    while True:
        cleaned = prefix.sub('', text, count=1).strip()
        if cleaned == text:
            break
        text = cleaned
    return re.sub(r'\s+', ' ', text).strip()

def _audible_search_candidates(query, context=None):
    context = context or {}
    terms = []
    playlist_title = clean_search_query(context.get('playlist_title') or '')
    album = clean_search_query(context.get('album') or '')
    if playlist_title:
        terms.append(playlist_title)
    if album and album != playlist_title:
        terms.append(album)
    for source in (context.get('artist'), context.get('uploader')):
        cleaned_source = clean_search_query(source or '')
        if cleaned_source and cleaned_source.casefold() not in {'unbekannt', 'unknown'}:
            terms.append(cleaned_source)
    for original in context.get('entry_titles') or []:
        cleaned = clean_search_query(original)
        if cleaned:
            terms.append(cleaned)
    normalized = clean_search_query(query)
    if normalized:
        terms.append(normalized)
        episode = re.search(r'\bFolge\s*\d+\b', normalized, flags=re.I)
        title = re.sub(r'[\(\[\{][^)]*[\)\]\}]', ' ', normalized)
        title = re.sub(r'\s+', ' ', title).strip(' -:;,')
        if episode and title:
            terms.append(f"{title} {episode.group(0)}")
        broad = re.sub(r'[\(\[\{][^)]*[\)\]\}]', ' ', normalized)
        broad = re.sub(
            r'\b(?:Kapitel|Teil|Part|Folge|Track|Episode)\s*\d+.*$',
            '', broad, flags=re.I)
        broad = re.sub(r'\s+', ' ', broad).strip(' -:;,')
        if broad:
            terms.append(broad)
    return list(dict.fromkeys(term for term in terms if term))


def _playlist_metadata_query(playlist_title, entries):
    return clean_search_query(playlist_title)


def _audible_tokens(value):
    value = html.unescape(str(value or '')).casefold()
    value = re.sub(r'<[^>]+>', ' ', value)
    return set(re.findall(r'[a-z0-9äöüß]+', value))


def _score_audible_product(product, query, context=None):
    context = context or {}
    title = str(context.get('playlist_title') or query or '')
    episode = re.search(r'\b(?:Folge\s*)?(\d+)\b', title, flags=re.I)
    series_target = ''
    if episode:
        series_target = re.split(r'\s+\d+\s*[-–:]', title, maxsplit=1)[0].strip(' -–:')
        if series_target == title:
            series_target = ''

    product_series = [
        str(item.get('title') or item.get('name') or '')
        for item in (product.get('series') or []) if isinstance(item, dict)
    ]
    publication = str(product.get('publication_name') or '')
    product_title = str(product.get('title') or '')
    subtitle = str(product.get('subtitle') or '')
    series_name = ' '.join(product_series)
    authors = ' '.join(
        str(item.get('name') or '') for item in (product.get('authors') or [])
        if isinstance(item, dict))
    entry_titles = [clean_search_query(x) for x in context.get('entry_titles') or []]
    entry_text = ' '.join(entry_titles)
    summary = clean_html(product.get('publisher_summary') or product.get('merchandising_summary') or '')

    series_tokens = _audible_tokens(series_target)
    matched_series = max((
        len(series_tokens & _audible_tokens(candidate)) / max(1, len(series_tokens))
        for candidate in product_series + [publication]
    ), default=0.0) if series_tokens else 0.0
    seq = next((str(item.get('sequence') or '') for item in (product.get('series') or [])
                if isinstance(item, dict) and item.get('sequence')), '')
    episode_match = bool(episode and seq and int(episode.group(1)) == int(re.sub(r'\D', '', seq) or -1))
    channel_tokens = _audible_tokens(context.get('uploader') or '') - {'topic', 'official'}
    author_match = bool(channel_tokens & _audible_tokens(authors))
    entry_tokens = _audible_tokens(entry_text) - {
        'intro', 'outro', 'kapitel', 'folge', 'track', 'episode', 'teil', 'part'
    }
    product_name_tokens = _audible_tokens(' '.join([product_title, subtitle, publication]))
    entry_name_overlap = (len(entry_tokens & product_name_tokens) / max(1, len(entry_tokens))) if entry_tokens else 0.0
    summary_overlap = (len(entry_tokens & _audible_tokens(summary)) / max(1, len(entry_tokens))) if entry_tokens else 0.0
    query_tokens = _audible_tokens(query) - {'der', 'die', 'das', 'des', 'und', 'ein', 'eine', 'folge', 'kapitel'}
    query_overlap = len(query_tokens & product_name_tokens) / max(1, len(query_tokens)) if query_tokens else 0.0

    score = (matched_series * 8) + (6 if episode_match else 0) + (3 if author_match else 0)
    score += entry_name_overlap * 3 + summary_overlap * 1.5 + query_overlap * 2
    if series_target:
        accepted = matched_series >= 0.6 and episode_match
    else:
        accepted = query_overlap >= 0.6 and bool(product_title or subtitle)
    return score, accepted


def fetch_audible_metadata(query, context=None):
    if not query or not str(query).strip():
        return None
    context = context or {}
    raw = clean_search_query(query)
    terms = _audible_search_candidates(query, context)
    headers = {'User-Agent': 'Mozilla/5.0'}
    products_by_asin = {}
    for term in terms:
        url = 'https://api.audible.de/1.0/catalog/products?' + urllib.parse.urlencode({
            'keywords': term,
            'num_results': '5',
            'products_sort_by': 'Relevance',
            'response_groups':
                'contributors,product_desc,product_attrs,product_extended_attrs,media,series'
        })
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=8) as response:
                products = json.loads(response.read().decode('utf-8', errors='replace')).get('products') or []
            for product in products:
                if not isinstance(product, dict):
                    continue
                key = product.get('asin') or json.dumps(product, sort_keys=True, ensure_ascii=False)
                products_by_asin[key] = product
        except Exception as error:
            print(f'Audible DE lookup failed for {term!r}: {error}')

    ranked = [(_score_audible_product(product, raw, context), product)
              for product in products_by_asin.values()]
    ranked = [(result, product) for result, product in ranked if result[1]]
    if not ranked:
        return None
    (_, _), product = max(ranked, key=lambda item: item[0][0])
    images = product.get('product_images') or {}
    cover = next((images.get(key) for key in ('1024', '800', '500')
                  if isinstance(images, dict) and images.get(key)), '')
    cover = cover or product.get('image') or product.get('cover_url') or ''

    def names(key):
        return ', '.join(item['name'] for item in (product.get(key) or [])
                         if isinstance(item, dict) and item.get('name'))

    series = next((item.get('title') or item.get('name')
                   for item in (product.get('series') or [])
                   if isinstance(item, dict) and (item.get('title') or item.get('name'))), '')
    release = str(product.get('release_date') or product.get('publication_name') or '')
    year = re.search(r'\b(?:19|20)\d{2}\b', release)
    chapters, asin = [], product.get('asin')
    if asin:
        for endpoint in (f'https://api.audnex.us/books/{asin}/chapters',
                         f'https://api.audnex.us/books/{asin}/chapters?region=de'):
            try:
                req = urllib.request.Request(endpoint, headers=headers)
                with urllib.request.urlopen(req, timeout=5) as response:
                    rows = json.loads(response.read().decode('utf-8', errors='replace')).get('chapters') or []
                chapters = [item['title'] for item in rows
                            if isinstance(item, dict) and item.get('title')]
                if chapters:
                    break
            except Exception as error:
                print(f'Audnex chapters lookup failed: {error}')
    description = product.get('publisher_summary') or product.get('merchandising_summary') or ''
    return {
        'title': product.get('title') or '',
        'author': names('authors'),
        'narrator': names('narrators'),
        'series': series,
        'year': year.group(0) if year else '',
        'cover_url': cover,
        'description': clean_html(description),
        'asin': asin,
        'audible_chapters': chapters
    }

@app.route('/info', methods=['POST'])
@app.route('/inspect', methods=['POST'])
def info():
    url = request.json.get('url')
    if not url:
        return jsonify({'error': 'Keine URL angegeben'}), 400

    cmd = [
        'yt-dlp',
        '--dump-single-json',
        '--flat-playlist',
        url
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", check=True)
        data = json.loads(res.stdout)
        
        is_playlist = 'entries' in data and data['entries'] is not None
        chapters = []
        
        if is_playlist:
            title = data.get('title') or data.get('playlist_title') or 'Unbekannte Playlist'
            uploader = (data.get('uploader') or data.get('channel') or
                        data.get('channel_title') or 'Unbekannt')
            album = data.get('album') or ''
            artist = data.get('artist') or data.get('artists') or ''
            entries = list(data.get('entries', []))
            
            for idx, entry in enumerate(entries):
                e_title = entry.get('title', f"Track {idx+1}")
                dur = entry.get('duration') or 0
                chapters.append({
                    'title': e_title,
                    'duration': dur,
                    'original': e_title
                })
            
            cover = data.get('thumbnails', [{}])[-1].get('url', '') if data.get('thumbnails') else ''
            if not cover and entries and entries[0].get('thumbnails'):
                cover = entries[0]['thumbnails'][-1].get('url', '')
                
            meta = fetch_audible_metadata(
                _playlist_metadata_query(title, entries),
                context={'playlist_title': title, 'album': album, 'artist': artist,
                         'entry_titles': [item.get('title', '') for item in entries if isinstance(item, dict)],
                         'uploader': uploader})
            
            return jsonify({
                'is_playlist': True,
                'title': meta.get('title') if meta and meta.get('title') else title,
                'author': meta.get('author') if meta and meta.get('author') else uploader,
                'series': meta.get('series') if meta and meta.get('series') else '',
                'year': meta.get('year') if meta and meta.get('year') else '',
                'cover_url': meta.get('cover_url') if meta and meta.get('cover_url') else cover,
                'thumbnail': meta.get('cover_url') if meta and meta.get('cover_url') else cover,
                'narrator': meta.get('narrator') if meta and meta.get('narrator') else '',
                'uploader': uploader,
                'description': meta.get('description') if meta and meta.get('description') else '',
                'chapters': chapters,
                'audible_chapters': meta.get('audible_chapters', []) if meta else []
            })
            
        else:
            title = data.get('title', 'Unbekannter Titel')
            uploader = data.get('uploader') or data.get('channel', 'Unbekannt')
            dur = data.get('duration') or 0
            
            raw_ch = data.get('chapters')
            if raw_ch:
                for c in raw_ch:
                    chapters.append({
                        'title': c.get('title', 'Kapitel'),
                        'start_time': c.get('start_time', 0),
                        'end_time': c.get('end_time', 0),
                        'original': c.get('title', 'Kapitel')
                    })
            else:
                chapters.append({
                    'title': title,
                    'duration': dur,
                    'original': title
                })
                
            cover = data.get('thumbnails', [{}])[-1].get('url', '') if data.get('thumbnails') else ''
            meta = fetch_audible_metadata(title)
            
            return jsonify({
                'is_playlist': False,
                'title': meta.get('title') if meta and meta.get('title') else title,
                'author': meta.get('author') if meta and meta.get('author') else uploader,
                'series': meta.get('series') if meta and meta.get('series') else '',
                'year': meta.get('year') if meta and meta.get('year') else '',
                'cover_url': meta.get('cover_url') if meta and meta.get('cover_url') else cover,
                'thumbnail': meta.get('cover_url') if meta and meta.get('cover_url') else cover,
                'narrator': meta.get('narrator') if meta and meta.get('narrator') else '',
                'uploader': uploader,
                'description': meta.get('description') if meta and meta.get('description') else '',
                'chapters': chapters,
                'audible_chapters': meta.get('audible_chapters', []) if meta else []
            })

    except subprocess.CalledProcessError as e:
        return jsonify({'error': f"yt-dlp Fehler: {e.stderr}"}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/lookup-meta', methods=['POST'])
def lookup_meta():
    data = request.get_json(silent=True) or {}
    meta = fetch_audible_metadata(data.get('query', ''))
    if not meta:
        return jsonify({'found': False})
    return jsonify({
        'found': True,
        'title': meta.get('title', ''),
        'author': meta.get('author', ''),
        'narrator': meta.get('narrator', ''),
        'series': meta.get('series', ''),
        'part': '',
        'year': meta.get('year', ''),
        'description': meta.get('description', ''),
        'cover': meta.get('cover_url', ''),
        'chapters': meta.get('audible_chapters', [])
    })


def _kill_subprocesses():
    with JOB_LOCK:
        procs = list(CURRENT_JOB.get("current_subprocesses") or [])
    for p in procs:
        if p and p.poll() is None:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except Exception:
                try:
                    p.terminate()
                except Exception:
                    pass
    time.sleep(0.5)
    for p in procs:
        if p and p.poll() is None:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass


@app.route('/cancel', methods=['POST'])
def cancel_job():
    with JOB_COND:
        if not CURRENT_JOB.get("active"):
            return jsonify({'success': False, 'message': 'Kein aktiver Job vorhanden'}), 400
        job_id = CURRENT_JOB.get("id")
        work_dir = CURRENT_JOB.get("work_dir")
        target_file = CURRENT_JOB.get("target_file")
        CURRENT_JOB["cancelled"] = True
        CURRENT_JOB["active"] = False

    _kill_subprocesses()

    if work_dir and os.path.exists(work_dir):
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception as err:
            print(f"Fehler beim Bereinigen von {work_dir}: {err}")

    if target_file and os.path.exists(target_file):
        try:
            os.remove(target_file)
        except Exception as err:
            print(f"Fehler beim Entfernen unfertiger Zieldatei {target_file}: {err}")

    try:
        for old_item in glob.glob(os.path.join(TMP_BASE_DIR, "work_*")):
            if os.path.isdir(old_item):
                shutil.rmtree(old_item, ignore_errors=True)
    except Exception:
        pass

    _emit_job_event({
        'status': 'cancelled',
        'msg': 'Job wurde vom Benutzer abgebrochen und temporäre Dateien gelöscht.',
        'replace_line': False
    })
    return jsonify({'success': True, 'message': 'Job abgebrochen und bereinigt'})


def _run_conversion_job(job_id, req_data, output_folder):
    url = req_data.get('url')
    title = req_data.get('title', 'Hörbuch').strip()
    author = req_data.get('author', 'Unbekannt').strip()
    narrator = req_data.get('narrator', '').strip()
    series = req_data.get('series', '').strip()
    year = req_data.get('year', '').strip()
    description = req_data.get('description', '').strip()
    cover_url = req_data.get('cover_url', '').strip()
    chapters = req_data.get('chapters', [])
    chapter_names = req_data.get('chapter_names', [])
    is_playlist = req_data.get('is_playlist', False)

    def get_chapter_title(idx):
        if idx < len(chapter_names) and isinstance(chapter_names[idx], str) and chapter_names[idx].strip():
            return chapter_names[idx].strip()
        if idx < len(chapters):
            ch = chapters[idx]
            if isinstance(ch, dict):
                return str(ch.get('title') or ch.get('original') or f"Kapitel {idx+1}").strip()
            if isinstance(ch, str) and ch.strip():
                return ch.strip()
        return f"Kapitel {idx+1}"

    tmpdir = tempfile.mkdtemp(prefix="work_", dir=TMP_BASE_DIR)
    proc = None
    ff_proc = None
    target_out_path = None

    with JOB_LOCK:
        CURRENT_JOB["work_dir"] = tmpdir
        CURRENT_JOB["current_subprocesses"] = []
        CURRENT_JOB["cancelled"] = False

    try:
        total_items = max(1, len(chapters)) if is_playlist else 1
        current_item = 1
        max_download_pct = 0.0
        current_item_max_pct = 0.0

        _emit_job_event({
            'status': 'downloading',
            'dl_progress': 0,
            'proc_progress': 0,
            'progress': 0,
            'msg': f'Starte Audio-Download ({total_items} Kapitel/Tracks)... — Gesamt: 0.0%',
            'replace_line': False
        })

        # 1. Download
        dl_cmd = [
            'yt-dlp',
            '--newline',
            '--progress',
            '-x',
            '--audio-format', 'm4a',
            '--audio-quality', '0',
            '--format', 'bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio/best',
            '--concurrent-fragments', '4',
            '--ignore-errors',
            '-o', f'{tmpdir}/%(playlist_index&{{:04d}}_|)s%(autonumber&{{:04d}}_|)s%(id)s.%(ext)s',
            url
        ]

        proc = subprocess.Popen(
            dl_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True
        )

        with JOB_LOCK:
            CURRENT_JOB["current_subprocesses"].append(proc)

        for line in iter(proc.stdout.readline, ''):
            if CURRENT_JOB.get("cancelled"):
                return

            m_item = re.search(r'\[download\]\s+Downloading\s+(?:item|video)\s+(\d+)\s+of\s+(\d+)', line, re.I)
            if m_item:
                current_item = int(m_item.group(1))
                total_items = max(1, int(m_item.group(2)))
                current_item_max_pct = 0.0
                ch_pct = (current_item - 1) / total_items * 100.0
                if ch_pct > max_download_pct:
                    max_download_pct = ch_pct
                ch_title = get_chapter_title(current_item - 1)
                _emit_job_event({
                    'status': 'downloading',
                    'dl_progress': round(max_download_pct, 1),
                    'proc_progress': 0,
                    'progress': int(max_download_pct * 0.5),
                    'msg': f'Lade Kapitel {current_item}/{total_items}: {ch_title} (0.0%) — Gesamt: {max_download_pct:.1f}%',
                    'replace_line': True
                })
                continue

            m = re.search(r'\[download\]\s+([\d\.]+)%', line)
            if m:
                p = float(m.group(1))
                if p > current_item_max_pct:
                    current_item_max_pct = p
                dl_fraction = min(1.0, max(0.0, current_item_max_pct / 100.0))
                item_overall_pct = ((current_item - 1) + (dl_fraction * 0.85)) / total_items * 100.0
                if item_overall_pct > max_download_pct:
                    max_download_pct = min(99.0, item_overall_pct)

                ch_title = get_chapter_title(current_item - 1)
                if total_items > 1:
                    msg_text = f"Lade Kapitel {current_item}/{total_items}: {ch_title} ({current_item_max_pct:.1f}%) — Gesamt: {max_download_pct:.1f}%"
                else:
                    msg_text = f"Download: {max_download_pct:.1f}% — Gesamt: {max_download_pct:.1f}%"

                _emit_job_event({
                    'status': 'downloading',
                    'dl_progress': round(max_download_pct, 1),
                    'proc_progress': 0,
                    'progress': int(max_download_pct * 0.5),
                    'msg': msg_text,
                    'replace_line': True
                })
                continue

            if '[ExtractAudio]' in line:
                ch_title = get_chapter_title(current_item - 1)
                conv_pct = (current_item / total_items) * 100.0
                if conv_pct > max_download_pct:
                    max_download_pct = min(100.0, conv_pct)
                msg_text = f"Konvertiere Kapitel {current_item}/{total_items}: {ch_title}... — Gesamt: {max_download_pct:.1f}%" if total_items > 1 else f"Konvertiere Audio nach M4A... — Gesamt: {max_download_pct:.1f}%"
                _emit_job_event({
                    'status': 'downloading',
                    'dl_progress': round(max_download_pct, 1),
                    'proc_progress': 0,
                    'progress': int(max_download_pct * 0.5),
                    'msg': msg_text,
                    'replace_line': True
                })
                continue

            log_line = line.strip()
            if log_line:
                _emit_job_event({
                    'status': 'downloading',
                    'msg': f'[yt-dlp] {log_line}',
                    'replace_line': False
                })

        proc.wait()

        if CURRENT_JOB.get("cancelled"):
            return

        downloaded = glob.glob(f"{tmpdir}/*.m4a")
        if proc.returncode != 0 and not downloaded:
            _emit_job_event({'status': 'error', 'msg': 'Download fehlgeschlagen'})
            return

        _emit_job_event({
            'status': 'processing',
            'dl_progress': 100,
            'proc_progress': 10,
            'progress': 55,
            'msg': 'Audio-Download abgeschlossen (100%). Lade Cover herunter...',
            'replace_line': False
        })

        if CURRENT_JOB.get("cancelled"):
            return

        # 2. Cover
        cover_file = None
        if cover_url:
            try:
                cover_file = os.path.join(tmpdir, "cover.jpg")
                c_req = urllib.request.Request(cover_url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(c_req, timeout=10) as c_resp, open(cover_file, 'wb') as cf:
                    cf.write(c_resp.read())
            except Exception as ce:
                print(f"Cover Download Fehler: {ce}")
                cover_file = None

        if CURRENT_JOB.get("cancelled"):
            return

        _emit_job_event({
            'status': 'processing',
            'dl_progress': 100,
            'proc_progress': 25,
            'progress': 65,
            'msg': 'Analysiere Audiodateien & berechne Kapitelmarken...',
            'replace_line': False
        })

        # 3. Audio verarbeiten
        def playlist_sort_key(path):
            match = re.match(r'(\d+)_', os.path.basename(path))
            return (0, int(match.group(1))) if match else (1, os.path.basename(path).casefold())

        audio_files = sorted(glob.glob(f"{tmpdir}/*.m4a"), key=playlist_sort_key)
        if not audio_files:
            _emit_job_event({'status': 'error', 'msg': 'Keine Audiodateien gefunden'})
            return

        safe_title = re.sub(r'[\/\\:\*?"<>\|]', '_', title)
        safe_title = safe_title.strip() or "Hörbuch"

        book_folder = os.path.join(output_folder, safe_title)
        os.makedirs(book_folder, exist_ok=True)

        out_filename = f"{safe_title}.m4b"
        out_path = os.path.join(book_folder, out_filename)
        target_out_path = out_path

        with JOB_LOCK:
            CURRENT_JOB["target_file"] = out_path

        def esc_ffmeta(val):
            s = str(val or '')
            s = re.sub(r'([=;#\\])', r'\\\1', s)
            return s.replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\\\n')

        total_duration_ms = 0

        ffmeta_path = os.path.join(tmpdir, "ffmetadata.txt")
        with open(ffmeta_path, "w", encoding="utf-8") as f:
            f.write(";FFMETADATA1\n")
            f.write(f"title={esc_ffmeta(title)}\n")
            f.write(f"artist={esc_ffmeta(author)}\n")
            f.write(f"album_artist={esc_ffmeta(author)}\n")
            f.write(f"album={esc_ffmeta(title)}\n")
            if narrator:
                f.write(f"composer={esc_ffmeta(narrator)}\n")
            if series:
                f.write(f"series={esc_ffmeta(series)}\n")
            if year:
                f.write(f"date={esc_ffmeta(year)}\n")
            if description:
                f.write(f"comment={esc_ffmeta(description)}\n")

            if is_playlist or len(audio_files) > 1:
                current_time_ms = 0
                num_files = len(audio_files)
                for idx, a_file in enumerate(audio_files):
                    if CURRENT_JOB.get("cancelled"):
                        return
                    p_cmd = [
                        'ffprobe', '-v', 'error',
                        '-show_entries', 'format=duration',
                        '-of', 'default=noprint_wrappers=1:nokey=1',
                        a_file
                    ]
                    dur_res = subprocess.run(p_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
                    try:
                        dur_sec = float(dur_res.stdout.strip() or 0)
                    except ValueError:
                        dur_sec = 0.0
                    dur_ms = int(dur_sec * 1000)

                    index_match = re.match(r'(\d+)_', os.path.basename(a_file))
                    original_idx = int(index_match.group(1)) - 1 if index_match else idx
                    ch_name = get_chapter_title(original_idx)
                    f.write("[CHAPTER]\nTIMEBASE=1/1000\n")
                    f.write(f"START={current_time_ms}\n")
                    f.write(f"END={current_time_ms + dur_ms}\n")
                    f.write(f"title={esc_ffmeta(ch_name)}\n")
                    current_time_ms += dur_ms

                    probe_pct = 25 + int(((idx + 1) / num_files) * 20)
                    _emit_job_event({
                        'status': 'processing',
                        'dl_progress': 100,
                        'proc_progress': probe_pct,
                        'progress': 65 + int(((idx + 1) / num_files) * 10),
                        'msg': f'Kapitel {idx+1}/{num_files} analysiert: {ch_name}',
                        'replace_line': idx > 0
                    })
                total_duration_ms = current_time_ms
            else:
                p_cmd = [
                    'ffprobe', '-v', 'error',
                    '-show_entries', 'format=duration',
                    '-of', 'default=noprint_wrappers=1:nokey=1',
                    audio_files[0]
                ]
                dur_res = subprocess.run(p_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
                try:
                    total_duration_ms = int(float(dur_res.stdout.strip() or 0) * 1000)
                except ValueError:
                    total_duration_ms = 0

                if chapters and isinstance(chapters[0], dict) and 'start_time' in chapters[0]:
                    for idx, c in enumerate(chapters):
                        s_ms = int(float(c.get('start_time', 0) or 0) * 1000)
                        e_ms = int(float(c.get('end_time', 0) or 0) * 1000)
                        ch_name = get_chapter_title(idx)
                        f.write("[CHAPTER]\nTIMEBASE=1/1000\n")
                        f.write(f"START={s_ms}\n")
                        f.write(f"END={e_ms}\n")
                        f.write(f"title={esc_ffmeta(ch_name)}\n")
                elif chapters:
                    f.write('[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\n')
                    f.write(f'END={total_duration_ms}\n')
                    f.write(f'title={esc_ffmeta(get_chapter_title(0))}\n')

        if CURRENT_JOB.get("cancelled"):
            return

        _emit_job_event({
            'status': 'processing',
            'dl_progress': 100,
            'proc_progress': 50,
            'progress': 80,
            'msg': 'Füge M4B zusammen und schreibe Kapitelmarken...',
            'replace_line': False
        })

        if is_playlist or len(audio_files) > 1:
            concat_list = os.path.join(tmpdir, "concat.txt")
            with open(concat_list, "w", encoding="utf-8") as cf:
                for af in audio_files:
                    safe_af = af.replace("'", "'\\''")
                    cf.write(f"file '{safe_af}'\n")

            ff_cmd = [
                'ffmpeg', '-y',
                '-f', 'concat', '-safe', '0', '-i', concat_list,
                '-i', ffmeta_path
            ]
        else:
            ff_cmd = [
                'ffmpeg', '-y',
                '-i', audio_files[0],
                '-i', ffmeta_path
            ]

        if cover_file:
            ff_cmd.extend(['-i', cover_file])

        ff_cmd.extend([
            '-map_metadata', '1',
            '-map', '0:a'
        ])
        if cover_file:
            ff_cmd.extend(['-map', '2:v', '-c:v', 'mjpeg', '-disposition:v', 'attached_pic'])

        ff_cmd.extend([
            '-c:a', 'copy',
            '-threads', '2',
            '-progress', 'pipe:1',
            '-nostats',
            out_path
        ])

        ff_proc = subprocess.Popen(
            ff_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True
        )

        with JOB_LOCK:
            CURRENT_JOB["current_subprocesses"].append(ff_proc)

        last_ff_pct = 50
        replaced_ff_line = False
        for line in iter(ff_proc.stdout.readline, ''):
            if CURRENT_JOB.get("cancelled"):
                return
            key_value = line.strip().split('=', 1)
            if len(key_value) == 2 and key_value[0] in ('bitrate', 'total_size', 'out_time', 'speed'):
                _emit_job_event({
                    'status': 'processing',
                    'msg': f'[ffmpeg] {key_value[0]}={key_value[1]}',
                    'replace_line': False
                })
            m_time = re.search(r'out_time_(?:us|ms)=(-?\d+)', line)
            if m_time and total_duration_ms > 0:
                out_us = max(0, int(m_time.group(1)))
                out_ms = out_us / 1000.0
                ratio = min(1.0, max(0.0, out_ms / total_duration_ms))
                proc_pct = 50 + int(ratio * 48)
                if proc_pct > last_ff_pct:
                    last_ff_pct = proc_pct
                    overall_p = 80 + int(ratio * 19)
                    _emit_job_event({
                        'status': 'processing',
                        'dl_progress': 100,
                        'proc_progress': proc_pct,
                        'progress': overall_p,
                        'msg': f'Schreibe M4B-Datei: {int(ratio * 100)}%',
                        'replace_line': replaced_ff_line
                    })
                    replaced_ff_line = True

        ff_proc.wait()

        if CURRENT_JOB.get("cancelled"):
            return

        if ff_proc.returncode != 0:
            retry_cmd = list(ff_cmd)
            codec_index = retry_cmd.index('-c:a')
            retry_cmd[codec_index + 1] = 'aac'
            retry_cmd[codec_index + 2:codec_index + 2] = ['-b:a', '128k', '-ar', '44100', '-ac', '2']
            _emit_job_event({
                'status': 'processing',
                'dl_progress': 100,
                'proc_progress': 50,
                'msg': 'Streamkopie fehlgeschlagen; starte AAC-Neucodierung mit 128 kbit/s ...',
                'replace_line': False
            })
            ff_proc = subprocess.Popen(
                retry_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True
            )
            with JOB_LOCK:
                CURRENT_JOB["current_subprocesses"].append(ff_proc)

            for retry_line in iter(ff_proc.stdout.readline, ''):
                if CURRENT_JOB.get("cancelled"):
                    return
                retry_text = retry_line.strip()
                if retry_text:
                    _emit_job_event({
                        'status': 'processing',
                        'msg': f'[ffmpeg/AAC] {retry_text}',
                        'replace_line': False
                    })
            ff_proc.wait()
            if ff_proc.returncode != 0:
                _emit_job_event({'status': 'error', 'msg': 'FFmpeg Streamkopie und AAC-Neucodierung fehlgeschlagen'})
                return

        if CURRENT_JOB.get("cancelled"):
            return

        _emit_job_event({
            'status': 'done',
            'dl_progress': 100,
            'proc_progress': 100,
            'progress': 100,
            'msg': f'Fertig! Gespeichert als {out_filename}',
            'filename': out_filename,
            'replace_line': False
        })

    except Exception as e:
        if not CURRENT_JOB.get("cancelled"):
            _emit_job_event({'status': 'error', 'msg': str(e)})
    finally:
        for active_proc in (ff_proc, proc):
            if active_proc is not None and active_proc.poll() is None:
                try:
                    active_proc.terminate()
                    active_proc.wait(timeout=2)
                except Exception:
                    try:
                        active_proc.kill()
                    except Exception:
                        pass

        if CURRENT_JOB.get("cancelled"):
            if target_out_path and os.path.exists(target_out_path):
                try:
                    os.remove(target_out_path)
                except Exception:
                    pass

        shutil.rmtree(tmpdir, ignore_errors=True)
        with JOB_COND:
            if CURRENT_JOB.get("id") == job_id and CURRENT_JOB.get("active"):
                CURRENT_JOB["active"] = False
                JOB_COND.notify_all()


def _stream_current_job(job_id):
    idx = 0
    while True:
        with JOB_COND:
            if CURRENT_JOB.get("id") != job_id:
                break
            events = CURRENT_JOB["events"]
            batch = events[idx:]
            idx = len(events)
            is_active = CURRENT_JOB.get("active", False)
            if not batch and is_active:
                JOB_COND.wait(timeout=2.0)
                events = CURRENT_JOB["events"]
                batch = events[idx:]
                idx = len(events)
                is_active = CURRENT_JOB.get("active", False)

        for ev in batch:
            yield f"data: {json.dumps(ev)}\n\n"
            if ev.get("status") in ("done", "error", "cancelled"):
                return

        if not batch:
            if not is_active:
                return
            yield ": keepalive\n\n"


@app.route('/job-status', methods=['GET'])
def job_status():
    with JOB_LOCK:
        return jsonify({
            "id": CURRENT_JOB["id"],
            "active": CURRENT_JOB["active"],
            "running": CURRENT_JOB["active"],
            "cancelled": CURRENT_JOB.get("cancelled", False),
            "event_count": len(CURRENT_JOB["events"]),
            "done": CURRENT_JOB["status"] in ("done", "error", "cancelled"),
            "status": CURRENT_JOB["status"],
            "title": CURRENT_JOB["title"],
            "dl_progress": CURRENT_JOB["dl_progress"],
            "proc_progress": CURRENT_JOB["proc_progress"],
            "progress": CURRENT_JOB["progress"],
            "dl_label": CURRENT_JOB["dl_label"],
            "updated_at": CURRENT_JOB["updated_at"],
        })


@app.route('/job-stream', methods=['GET'])
def job_stream():
    with JOB_LOCK:
        job_id = CURRENT_JOB.get("id")
        if not job_id:
            return Response("data: {}\n\n", mimetype='text/event-stream')
    return Response(
        _stream_current_job(job_id),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache, no-transform',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive'
        }
    )


@app.route('/process', methods=['POST'])
def process():
    req_data = request.get_json(silent=True) or {}
    try:
        output_folder = resolve_destination(req_data.get('destination_folder', ''))
    except ValueError as error:
        return jsonify({'error': str(error)}), 400

    with JOB_COND:
        if CURRENT_JOB.get("active"):
            job_id = CURRENT_JOB["id"]
            already_running = True
        else:
            job_id = uuid.uuid4().hex
            CURRENT_JOB.update({
                "id": job_id,
                "active": True,
                "cancelled": False,
                "status": "downloading",
                "title": (req_data.get("title") or "Hörbuch").strip(),
                "dl_progress": 0,
                "proc_progress": 0,
                "progress": 0,
                "dl_label": "1. Download-Fortschritt (Gesamtes Buch)",
                "events": [],
                "updated_at": time.time(),
                "work_dir": None,
                "current_subprocesses": [],
                "target_file": None,
            })
            already_running = False

    if not already_running:
        worker = threading.Thread(
            target=_run_conversion_job,
            args=(job_id, req_data, output_folder),
            daemon=True
        )
        worker.start()

    return Response(
        _stream_current_job(job_id),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache, no-transform',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive'
        }
    )



# === NEXTORY integration ===
def _nextory_service():
    import nextory_service
    return nextory_service

@app.route('/api/nextory/status', methods=['GET'])
def nextory_status():
    try:
        return jsonify(_nextory_service().status())
    except Exception as error:
        return jsonify({'error': str(error)}), 500

@app.route('/api/nextory/login', methods=['POST'])
def nextory_login():
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(_nextory_service().login(data.get('username', ''), data.get('password', ''), data.get('country', 'DE')))
    except Exception as error:
        return jsonify({'error': str(error)}), 400

@app.route('/api/nextory/select-profile', methods=['POST'])
def nextory_select_profile():
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(_nextory_service().select_profile(data.get('login_key', '')))
    except Exception as error:
        return jsonify({'error': str(error)}), 400

@app.route('/api/nextory/logout', methods=['POST'])
def nextory_logout():
    try:
        _nextory_service().logout()
        return jsonify({'ok': True})
    except Exception as error:
        return jsonify({'error': str(error)}), 400

@app.route('/api/nextory/search', methods=['GET'])
def nextory_search():
    try:
        return jsonify(_nextory_service().search(request.args.get('q', '').strip()))
    except Exception as error:
        return jsonify({'error': str(error)}), 400

@app.route('/api/nextory/library', methods=['GET'])
def nextory_library():
    try:
        return jsonify(_nextory_service().library())
    except Exception as error:
        return jsonify({'error': str(error)}), 400

@app.route('/api/nextory/book/<int:book_id>', methods=['GET'])
def nextory_book(book_id):
    try:
        return jsonify(_nextory_service().book(book_id))
    except Exception as error:
        return jsonify({'error': str(error)}), 400

def _run_nextory_job(job_id, payload, output_folder):
    try:
        def progress(message, percent, phase='downloading'):
            if phase == 'processing':
                _emit_job_event({'status': 'processing', 'dl_progress': 100,
                                 'proc_progress': percent, 'progress': percent,
                                 'msg': f'{message} ({percent:.0f}%)'})
            else:
                _emit_job_event({'status': 'downloading', 'dl_progress': percent,
                                 'proc_progress': 0, 'progress': percent,
                                 'msg': f'{message} — Download: {percent:.1f}%'})
        result = _nextory_service().download(payload, output_folder, progress)
        _emit_job_event({'status': 'done', 'dl_progress': 100, 'proc_progress': 100,
                         'progress': 100, 'msg': f'Nextory-Hörbuch gespeichert: {result}'})
    except Exception as error:
        _emit_job_event({'status': 'error', 'msg': f'Nextory-Download fehlgeschlagen: {error}'})
    finally:
        with JOB_COND:
            CURRENT_JOB['work_dir'] = None
            CURRENT_JOB['current_subprocesses'] = []
            JOB_COND.notify_all()

@app.route('/api/nextory/download', methods=['POST'])
def nextory_download():
    payload = request.get_json(silent=True) or {}
    try:
        output_folder = resolve_destination(payload.get('destination_folder', ''))
    except ValueError as error:
        return jsonify({'error': str(error)}), 400
    if not payload.get('book_id') and not payload.get('id'):
        return jsonify({'error': 'book_id is required'}), 400
    with JOB_COND:
        if CURRENT_JOB.get('active'):
            return jsonify({'error': 'Es läuft bereits ein Download-Auftrag.'}), 409
        job_id = uuid.uuid4().hex
        CURRENT_JOB.update({'id': job_id, 'active': True, 'cancelled': False, 'status': 'downloading',
                            'title': (payload.get('title') or 'Nextory-Hörbuch').strip(),
                            'dl_progress': 0, 'proc_progress': 0, 'progress': 0,
                            'dl_label': 'Nextory-Download-Fortschritt', 'events': [],
                            'updated_at': time.time(), 'work_dir': None,
                            'current_subprocesses': [], 'target_file': None})
    worker = threading.Thread(target=_run_nextory_job, args=(job_id, payload, output_folder), daemon=True)
    worker.start()
    return Response(_stream_current_job(job_id), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache, no-transform', 'X-Accel-Buffering': 'no',
                             'Connection': 'keep-alive'})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)

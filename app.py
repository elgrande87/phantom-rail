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
from folder_support import register_folders

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
    "status": "idle",
    "title": "",
    "dl_progress": 0,
    "proc_progress": 0,
    "progress": 0,
    "dl_label": "1. Download-Fortschritt (Gesamtes Buch)",
    "events": [],
    "updated_at": 0.0,
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

        if st in ("done", "error"):
            CURRENT_JOB["active"] = False
        CURRENT_JOB["updated_at"] = time.time()
        JOB_COND.notify_all()


@app.route('/')
def index():
    return render_template('index.html')


def clean_html(raw_html):
    if not raw_html:
        return ''
    return html.unescape(re.sub(r'<[^>]+>', '', str(raw_html))).strip()

def clean_search_query(query):
    """Strip leading track labels without discarding episode identifiers."""
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

def _audible_search_candidates(query):
    """Try exact episode terms first; only then broaden to the base title."""
    normalized = clean_search_query(query)
    if not normalized:
        return []
    episode = re.search(r'\bFolge\s*\d+\b', normalized, flags=re.I)
    title = re.sub(r'[\(\[\{][^)]*[\)\]\}]', ' ', normalized)
    title = re.sub(r'\s+', ' ', title).strip(' -:;,')
    terms = []
    if episode and title:
        terms.append(f"{title} {episode.group(0)}")
    terms.append(normalized)
    broad = re.sub(r'[\(\[\{][^)]*[\)\]\}]', ' ', normalized)
    broad = re.sub(
        r'\b(?:Kapitel|Teil|Part|Folge|Track|Episode)\s*\d+.*$',
        '', broad, flags=re.I)
    broad = re.sub(r'\s+', ' ', broad).strip(' -:;,')
    if broad:
        terms.append(broad)
    return list(dict.fromkeys(term for term in terms if term))


def _playlist_metadata_query(playlist_title, entries):
    """Prefer a cleaned playlist track title carrying its exact Folge number."""
    candidates = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        original = str(entry.get('title') or '').strip()
        candidate = clean_search_query(original)
        if candidate and candidate != original:
            candidates.append(candidate)
    for candidate in candidates:
        if re.search(r'\bFolge\s*\d+\b', candidate, flags=re.I):
            return candidate
    return candidates[0] if candidates else clean_search_query(playlist_title)

def fetch_audible_metadata(query):
    """Load metadata from Audible Germany."""
    if not query or not str(query).strip():
        return None
    raw = clean_search_query(query)
    terms = _audible_search_candidates(query)
    headers = {'User-Agent': 'Mozilla/5.0'}
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
                products = json.loads(response.read().decode('utf-8')).get('products') or []
            if not products:
                continue
            product = products[0]
            images = product.get('product_images') or {}
            cover = next((
                images.get(key) for key in ('1024', '800', '500')
                if isinstance(images, dict) and images.get(key)
            ), '')
            cover = cover or product.get('image') or product.get('cover_url') or ''
            def names(key):
                return ', '.join(
                    item['name'] for item in (product.get(key) or [])
                    if isinstance(item, dict) and item.get('name'))
            series = next((
                item.get('title') or item.get('name')
                for item in (product.get('series') or [])
                if isinstance(item, dict) and (item.get('title') or item.get('name'))
            ), '')
            release = str(product.get('release_date') or product.get('publication_name') or '')
            year = re.search(r'\b(?:19|20)\d{2}\b', release)
            chapters, asin = [], product.get('asin')
            if asin:
                for endpoint in (
                    f'https://api.audnex.us/books/{asin}/chapters',
                    f'https://api.audnex.us/books/{asin}/chapters?region=de'
                ):
                    try:
                        req = urllib.request.Request(endpoint, headers=headers)
                        with urllib.request.urlopen(req, timeout=5) as response:
                            rows = json.loads(response.read().decode('utf-8')).get('chapters') or []
                        chapters = [
                            item['title'] for item in rows
                            if isinstance(item, dict) and item.get('title')]
                        if chapters:
                            break
                    except Exception as error:
                        print(f'Audnex chapters lookup failed: {error}')
            description = product.get('publisher_summary') or product.get('merchandising_summary') or ''
            return {
                'title': product.get('title') or raw,
                'author': names('authors'),
                'narrator': names('narrators'),
                'series': series,
                'year': year.group(0) if year else '',
                'cover_url': cover,
                'description': clean_html(description),
                'asin': asin,
                'audible_chapters': chapters
            }
        except Exception as error:
            print(f'Audible DE lookup failed for {term!r}: {error}')
    return None

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
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        data = json.loads(res.stdout)
        
        is_playlist = 'entries' in data and data['entries'] is not None
        chapters = []
        
        if is_playlist:
            title = data.get('title', 'Unbekannte Playlist')
            uploader = data.get('uploader') or data.get('channel', 'Unbekannt')
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
                
            # YouTube Music OLAK (album) playlists are titled with the series and
            # episode number, while their first track is often the actual book title
            # prefixed by "Intro:". Searching the playlist title can match a different
            # episode in Audible's relevance ordering, so prefer the normalized track.
            search_title = title
            for entry in entries:
                entry_title = entry.get('title', '')
                candidate = clean_search_query(entry_title)
                if candidate != entry_title.strip() and candidate:
                    search_title = candidate
                    break
            meta = fetch_audible_metadata(_playlist_metadata_query(title, entries))
            
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
    try:
        total_items = max(1, len(chapters)) if is_playlist else 1
        current_item = 1
        last_overall_pct = -1.0
        last_item_reported = 0

        _emit_job_event({
            'status': 'downloading',
            'dl_progress': 0,
            'proc_progress': 0,
            'progress': 0,
            'msg': f'Starte Audio-Download ({total_items} Kapitel/Tracks)...',
            'replace_line': False
        })

        # 1. Download
        dl_cmd = [
            'yt-dlp',
            '-x',
            '--audio-format', 'm4a',
            '--audio-quality', '0',
            '--ignore-errors',
            '-o', f'{tmpdir}/%(playlist_index&{{:04d}}_|)s%(autonumber&{{:04d}}_|)s%(id)s.%(ext)s',
            '--newline',
            url
        ]

        proc = subprocess.Popen(dl_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in iter(proc.stdout.readline, ''):
            m_item = re.search(r'\[download\]\s+Downloading\s+(?:item|video)\s+(\d+)\s+of\s+(\d+)', line, re.I)
            if m_item:
                current_item = int(m_item.group(1))
                total_items = max(1, int(m_item.group(2)))
                ch_title = get_chapter_title(current_item - 1)
                overall_pct = round(((current_item - 1) / total_items) * 100, 1)
                _emit_job_event({
                    'status': 'downloading',
                    'dl_progress': overall_pct,
                    'proc_progress': 0,
                    'progress': int(overall_pct * 0.5),
                    'msg': f'Lade Kapitel {current_item}/{total_items}: {ch_title} (0.0%) — Gesamt: {overall_pct:.1f}%',
                    'replace_line': False
                })
                last_item_reported = current_item
                continue

            m = re.search(r'\[download\]\s+([\d\.]+)%', line)
            if m:
                p = float(m.group(1))
                overall_pct = ((current_item - 1) + (p / 100.0)) / total_items * 100.0
                overall_pct = min(100.0, max(0.0, overall_pct))
                if abs(overall_pct - last_overall_pct) >= 0.3 or p >= 100.0 or current_item != last_item_reported:
                    last_overall_pct = overall_pct
                    replace_flag = (current_item == last_item_reported)
                    last_item_reported = current_item
                    ch_title = get_chapter_title(current_item - 1)
                    if total_items > 1:
                        msg_text = f"Lade Kapitel {current_item}/{total_items}: {ch_title} ({p:.1f}%) — Gesamt: {overall_pct:.1f}%"
                    else:
                        msg_text = f"Download: {overall_pct:.1f}%"
                    _emit_job_event({
                        'status': 'downloading',
                        'dl_progress': round(overall_pct, 1),
                        'proc_progress': 0,
                        'progress': int(overall_pct * 0.5),
                        'msg': msg_text,
                        'replace_line': replace_flag
                    })
                continue

            if '[ExtractAudio]' in line:
                ch_title = get_chapter_title(current_item - 1)
                overall_pct = round((current_item / total_items) * 100, 1)
                msg_text = f"Konvertiere Kapitel {current_item}/{total_items}: {ch_title}..." if total_items > 1 else "Konvertiere Audio nach M4A..."
                _emit_job_event({
                    'status': 'downloading',
                    'dl_progress': overall_pct,
                    'proc_progress': 0,
                    'progress': int(overall_pct * 0.5),
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

        safe_title = re.sub(r'[\/\\:\*\?"<>\|]', '_', title)
        safe_title = safe_title.strip() or "Hörbuch"

        # Eigener Ordner pro Buch im gewaehlten Zielordner anlegen
        book_folder = os.path.join(output_folder, safe_title)
        os.makedirs(book_folder, exist_ok=True)

        out_filename = f"{safe_title}.m4b"
        out_path = os.path.join(book_folder, out_filename)

        def esc_ffmeta(val):
            s = str(val or '')
            s = re.sub(r'([=;#\\])', r'\\\1', s)
            return s.replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\\\n')

        total_duration_ms = 0

        # Metadata File fuer ffmpeg
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
                    p_cmd = [
                        'ffprobe', '-v', 'error',
                        '-show_entries', 'format=duration',
                        '-of', 'default=noprint_wrappers=1:nokey=1',
                        a_file
                    ]
                    dur_res = subprocess.run(p_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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
                dur_res = subprocess.run(p_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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

        _emit_job_event({
            'status': 'processing',
            'dl_progress': 100,
            'proc_progress': 50,
            'progress': 80,
            'msg': 'Füge M4B zusammen und schreibe Kapitelmarken...',
            'replace_line': False
        })

        # FFmpeg M4B Zusammenfuehrung
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

        ff_proc = subprocess.Popen(ff_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        last_ff_pct = 50
        replaced_ff_line = False
        for line in iter(ff_proc.stdout.readline, ''):
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
            ff_proc = subprocess.Popen(retry_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            for retry_line in iter(ff_proc.stdout.readline, ''):
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
        _emit_job_event({'status': 'error', 'msg': str(e)})
    finally:
        for active_proc in (ff_proc, proc):
            if active_proc is not None and active_proc.poll() is None:
                try:
                    active_proc.terminate()
                    active_proc.wait(timeout=3)
                except Exception:
                    try:
                        active_proc.kill()
                        active_proc.wait(timeout=3)
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
            if ev.get("status") in ("done", "error"):
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
            "event_count": len(CURRENT_JOB["events"]),
            "done": CURRENT_JOB["status"] in ("done", "error"),
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
            # Falls bereits ein Job laeuft, einfach an den laufenden Job anhaengen!
            job_id = CURRENT_JOB["id"]
            already_running = True
        else:
            job_id = uuid.uuid4().hex
            CURRENT_JOB.update({
                "id": job_id,
                "active": True,
                "status": "downloading",
                "title": (req_data.get("title") or "Hörbuch").strip(),
                "dl_progress": 0,
                "proc_progress": 0,
                "progress": 0,
                "dl_label": "1. Download-Fortschritt (Gesamtes Buch)",
                "events": [],
                "updated_at": time.time(),
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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)

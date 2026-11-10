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

app = Flask(__name__)
DOWNLOAD_FOLDER = os.environ.get("DOWNLOAD_FOLDER", "/downloads")
TMP_BASE_DIR = os.path.join(DOWNLOAD_FOLDER, ".tmp_work")

os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(TMP_BASE_DIR, exist_ok=True)

# Alte temporaere Reste beim Start bereinigen
try:
    for old_item in glob.glob(os.path.join(TMP_BASE_DIR, "work_*")):
        if os.path.isdir(old_item):
            shutil.rmtree(old_item, ignore_errors=True)
except Exception as e:
    print(f"Konnte alte Temp-Ordner nicht aufraeumen: {e}")

@app.route('/')
def index():
    return render_template('index.html')

def fetch_audnexus_metadata(query):
    try:
        clean_q = re.sub(r'[\(\[\{].*?[\)\]\}]', '', query)
        clean_q = re.sub(r'(?:Kapitel|Teil|Part|Folge|Track|Episode)\s*\d+.*$', '', clean_q, flags=re.I)
        clean_q = re.sub(r'\s+', ' ', clean_q).strip()
        encoded = urllib.parse.quote(clean_q)
        
        search_url = f"https://api.audnex.us/books?title={encoded}&limit=1"
        req = urllib.request.Request(search_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            if data and len(data) > 0:
                book = data[0]
                asin = book.get('asin')
                authors = ", ".join([a.get('name') for a in book.get('authors', [])]) if book.get('authors') else ""
                series_list = [f"{s.get('name')} #{s.get('position', '')}".strip() for s in book.get('series', [])]
                series = ", ".join(series_list) if series_list else ""
                release_date = book.get('releaseDate', '')
                year = release_date.split('-')[0] if release_date else ""
                image = book.get('image', '')
                description = book.get('summary', '') or book.get('description', '')
                
                chapters = []
                if asin:
                    try:
                        ch_url = f"https://api.audnex.us/books/{asin}/chapters"
                        ch_req = urllib.request.Request(ch_url, headers={'User-Agent': 'Mozilla/5.0'})
                        with urllib.request.urlopen(ch_req, timeout=5) as ch_resp:
                            ch_data = json.loads(ch_resp.read().decode('utf-8'))
                            if ch_data and 'chapters' in ch_data:
                                chapters = [c.get('title') for c in ch_data['chapters'] if c.get('title')]
                    except Exception:
                        pass
                
                return {
                    'title': book.get('title'),
                    'author': authors,
                    'series': series,
                    'year': year,
                    'cover_url': image,
                    'description': description,
                    'audible_chapters': chapters
                }
    except Exception as e:
        print(f"Audnexus-Suche fehlgeschlagen: {e}")
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
                
            meta = fetch_audnexus_metadata(title)
            
            return jsonify({
                'is_playlist': True,
                'title': meta.get('title') if meta and meta.get('title') else title,
                'author': meta.get('author') if meta and meta.get('author') else uploader,
                'series': meta.get('series') if meta and meta.get('series') else '',
                'year': meta.get('year') if meta and meta.get('year') else '',
                'cover_url': meta.get('cover_url') if meta and meta.get('cover_url') else cover,
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
            meta = fetch_audnexus_metadata(title)
            
            return jsonify({
                'is_playlist': False,
                'title': meta.get('title') if meta and meta.get('title') else title,
                'author': meta.get('author') if meta and meta.get('author') else uploader,
                'series': meta.get('series') if meta and meta.get('series') else '',
                'year': meta.get('year') if meta and meta.get('year') else '',
                'cover_url': meta.get('cover_url') if meta and meta.get('cover_url') else cover,
                'description': meta.get('description') if meta and meta.get('description') else '',
                'chapters': chapters,
                'audible_chapters': meta.get('audible_chapters', []) if meta else []
            })

    except subprocess.CalledProcessError as e:
        return jsonify({'error': f"yt-dlp Fehler: {e.stderr}"}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/process', methods=['POST'])
def process():
    req_data = request.json
    url = req_data.get('url')
    title = req_data.get('title', 'Hörbuch').strip()
    author = req_data.get('author', 'Unbekannt').strip()
    series = req_data.get('series', '').strip()
    year = req_data.get('year', '').strip()
    description = req_data.get('description', '').strip()
    cover_url = req_data.get('cover_url', '').strip()
    chapters = req_data.get('chapters', [])
    is_playlist = req_data.get('is_playlist', False)

    def generate():
        tmpdir = tempfile.mkdtemp(prefix="work_", dir=TMP_BASE_DIR)
        try:
            yield f"data: {json.dumps({'status': 'downloading', 'progress': 5, 'msg': 'Starte Audio-Download...'})}\n\n"
            
            # 1. Download
            dl_cmd = [
                'yt-dlp',
                '-x',
                '--audio-format', 'm4a',
                '--audio-quality', '0',
                '-o', f'{tmpdir}/%(playlist_index&{{:03d}}_|)s%(autonumber&{{:03d}}_|)s%(id)s.%(ext)s',
                '--newline',
                url
            ]
            
            proc = subprocess.Popen(dl_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in proc.stdout:
                m = re.search(r'\[download\]\s+([\d\.]+)%', line)
                if m:
                    p = float(m.group(1))
                    scaled_p = 5 + int(p * 0.45)
                    yield f"data: {json.dumps({'status': 'downloading', 'progress': scaled_p, 'msg': f'Download: {p:.1f}%'})}\n\n"
            proc.wait()
            
            if proc.returncode != 0:
                yield f"data: {json.dumps({'status': 'error', 'msg': 'Download fehlgeschlagen'})}\n\n"
                return

            yield f"data: {json.dumps({'status': 'processing', 'progress': 55, 'msg': 'Lade Cover herunter...'})}\n\n"
            
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

            yield f"data: {json.dumps({'status': 'processing', 'progress': 65, 'msg': 'Analysiere Audiodateien...'})}\n\n"

            # 3. Audio verarbeiten
            audio_files = sorted(glob.glob(f"{tmpdir}/*.m4a"))
            if not audio_files:
                yield f"data: {json.dumps({'status': 'error', 'msg': 'Keine Audiodateien gefunden'})}\n\n"
                return

            safe_title = re.sub(r'[\/\\:\*\?"<>\|]', '_', title)
            out_filename = f"{safe_title}.m4b"
            out_path = os.path.join(DOWNLOAD_FOLDER, out_filename)

            # Metadata File fuer ffmpeg
            ffmeta_path = os.path.join(tmpdir, "ffmetadata.txt")
            with open(ffmeta_path, "w", encoding="utf-8") as f:
                f.write(";FFMETADATA1\n")
                f.write(f"title={title}\n")
                f.write(f"artist={author}\n")
                f.write(f"album_artist={author}\n")
                f.write(f"album={title}\n")
                if series:
                    f.write(f"series={series}\n")
                if year:
                    f.write(f"date={year}\n")
                if description:
                    f.write(f"comment={description}\n")

                if is_playlist:
                    current_time_ms = 0
                    for idx, a_file in enumerate(audio_files):
                        p_cmd = [
                            'ffprobe', '-v', 'error',
                            '-show_entries', 'format=duration',
                            '-of', 'default=noprint_wrappers=1:nokey=1',
                            a_file
                        ]
                        dur_res = subprocess.run(p_cmd, stdout=subprocess.PIPE, text=True)
                        dur_sec = float(dur_res.stdout.strip() or 0)
                        dur_ms = int(dur_sec * 1000)
                        
                        ch_name = chapters[idx] if idx < len(chapters) else f"Kapitel {idx+1}"
                        f.write("[CHAPTER]\nTIMEBASE=1/1000\n")
                        f.write(f"START={current_time_ms}\n")
                        f.write(f"END={current_time_ms + dur_ms}\n")
                        f.write(f"title={ch_name}\n")
                        current_time_ms += dur_ms
                else:
                    if chapters and len(chapters) > 1 and isinstance(chapters[0], dict) and 'start_time' in chapters[0]:
                        for c in chapters:
                            s_ms = int(c.get('start_time', 0) * 1000)
                            e_ms = int(c.get('end_time', 0) * 1000)
                            f.write("[CHAPTER]\nTIMEBASE=1/1000\n")
                            f.write(f"START={s_ms}\n")
                            f.write(f"END={e_ms}\n")
                            f.write(f"title={c.get('title', 'Kapitel')}\n")

            yield f"data: {json.dumps({'status': 'processing', 'progress': 80, 'msg': 'Füge M4B zusammen und binde Tags ein...'})}\n\n"

            # FFmpeg M4B Zusammenfuehrung
            if is_playlist:
                concat_list = os.path.join(tmpdir, "concat.txt")
                with open(concat_list, "w", encoding="utf-8") as cf:
                    for af in audio_files:
                        cf.write(f"file '{af}'\n")

                ff_cmd = [
                    'ffmpeg', '-y',
                    '-f', 'concat', '-safe', '0', '-i', concat_list,
                    '-i', ffmeta_path
                ]
                if cover_file:
                    ff_cmd.extend(['-i', cover_file])

                ff_cmd.extend([
                    '-map_metadata', '1',
                    '-map', '0:a'
                ])
                if cover_file:
                    ff_cmd.extend(['-map', '2:v', '-disposition:v', 'attached_pic'])

                ff_cmd.extend([
                    '-c:a', 'copy',
                    '-threads', '2',
                    out_path
                ])
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
                    ff_cmd.extend(['-map', '2:v', '-disposition:v', 'attached_pic'])

                ff_cmd.extend([
                    '-c:a', 'copy',
                    '-threads', '2',
                    out_path
                ])

            ff_res = subprocess.run(ff_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if ff_res.returncode != 0:
                yield f"data: {json.dumps({'status': 'error', 'msg': f'FFmpeg Fehler: {ff_res.stderr[-200:]}'})}\n\n"
                return

            yield f"data: {json.dumps({'status': 'done', 'progress': 100, 'msg': f'Fertig! Gespeichert als {out_filename}', 'filename': out_filename})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'status': 'error', 'msg': str(e)})}\n\n"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    return Response(generate(), mimetype='text/event-stream')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)

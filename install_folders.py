"""Run once on the host: python3 install_folders.py (safe to repeat)."""
from pathlib import Path
import shutil

base = Path(__file__).resolve().parent
changes = {}

def replace_checked(text, old, new):
    if old not in text:
        raise SystemExit('Installation abgebrochen: erwartete Code-Stelle fehlt: ' + old[:100])
    return text.replace(old, new, 1)

p = base / 'app.py'
text = p.read_text(encoding='utf-8')
if 'from folder_support import register_folders' not in text:
    text = replace_checked(text, 'os.makedirs(TMP_BASE_DIR, exist_ok=True)', 'os.makedirs(TMP_BASE_DIR, exist_ok=True)\n\nfrom folder_support import register_folders\nresolve_destination = register_folders(app, DOWNLOAD_FOLDER)')
    text = replace_checked(text, "    req_data = request.json\n    url = req_data.get('url')", "    req_data = request.get_json(silent=True) or {}\n    try:\n        output_folder = resolve_destination(req_data.get('destination_folder', ''))\n    except ValueError as error:\n        return jsonify({'error': str(error)}), 400\n    url = req_data.get('url')")
    text = replace_checked(text, 'out_path = os.path.join(DOWNLOAD_FOLDER, out_filename)', 'out_path = os.path.join(output_folder, out_filename)')
    text = replace_checked(text, 'out_filename = f"{safe_title}.m4b"', 'safe_title = safe_title.strip() or "Hörbuch"\n            out_filename = f"{safe_title}.m4b"')
    changes[p] = text

p = base / 'templates' / 'index.html'
text = p.read_text(encoding='utf-8')
if '/static/folders.js' not in text:
    text = replace_checked(text, 'async function startDownload() {', "async function startDownload() {\n    if (!window.foldersReady) {alert('Bitte erst die Speicherordner laden (Aktualisieren).'); return;}")
    text = replace_checked(text, '        is_playlist:globalIsPlaylist', "        destination_folder:document.getElementById('destinationFolder').value,\n        is_playlist:globalIsPlaylist")
    text = replace_checked(text, '</body>', '<script src="/static/folders.js"></script>\n</body>')
    changes[p] = text

p = base / 'docker-compose.yml'
text = p.read_text(encoding='utf-8')
if '/mnt/ssd/audiobooks:/downloads' not in text and '/mnt/dietpi_userdata/audiobooks:/downloads' in text:
    text = replace_checked(text, '/mnt/dietpi_userdata/audiobooks:/downloads', '/mnt/ssd/audiobooks:/downloads')
    changes[p] = text

if base / 'app.py' in changes:
    compile(changes[base / 'app.py'], str(base / 'app.py'), 'exec')
for path, content in changes.items():
    backup = path.with_name(path.name + '.before-folders')
    if not backup.exists():
        shutil.copy2(path, backup)
    path.write_text(content, encoding='utf-8')
    print('Aktualisiert:', path.relative_to(base))
print('Ordnerauswahl ist bereits aktiv. Jetzt: docker compose up -d --build')

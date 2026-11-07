# Audio Downloader (Docker für DietPi / Radxa Zero 3W)

Web-Oberfläche zum Prüfen von Hörbuch-URLs, Voransicht & Bearbeiten von Metadaten und Kapiteln (inkl. Audnexus/Audible-Suche & Massen-Bearbeitungstools) und Zusammenfügen als `.m4b` via `yt-dlp` und `ffmpeg`.

Speziell optimiert für Boards mit geringem Arbeitsspeicher (1 GB RAM wie beim Radxa Zero 3W / Raspberry Pi Zero 2W):
- Temporäre Arbeitsdateien werden auf dem gemounteten Speicher (`/downloads/.tmp_work`) statt in der RAM-Disk (`/tmp`) abgelegt.
- `ffmpeg` ist auf 2 Threads begrenzt, um CPU und RAM stabil zu halten.
- Gunicorn läuft mit 1 Worker und 4 Threads.

## Schnellstart auf DietPi

1. **Docker installieren (falls nicht vorhanden):**
   ```bash
   sudo dietpi-software install 162 134
   ```

2. **Repository klonen:**
   ```bash
   git clone https://github.com/elgrande87/audio-downloader.git
   cd audio-downloader
   ```

3. **Container starten:**
   ```bash
   docker compose up -d --build
   ```

4. **Im Browser aufrufen:**
   ```text
   http://<IP-DEINES-BOARDS>:5000
   ```

<p align="center">
  <img src="static/logo.svg" alt="phantom-rail Logo" width="160">
</p>

# 👻🚂 phantom-rail

> Web-Oberfläche & Pipeline zum Herunterladen, Taggen und Binden von Hörbüchern als `.m4b` mit Kapiteln (optimiert für SBCs wie Radxa Zero 3W, DietPi & Raspberry Pi).

## Features
- 🔍 **Metadaten- & Kapitelsuche:** Automatischer Abgleich via Audnexus & Audible.
- ✂️ **Batch-Editor:** Schnelles Bereinigen von Kapitelnamen und Textmustern.
- 📦 **M4B-Container:** Bündelt Audio, Cover-Artworks und Kapitelmarken via `ffmpeg`.
- 🪶 **Low-RAM-Optimierung:** Speziell angepasst an Einplatinencomputer (z. B. 1 GB RAM):
  - Temporäre Daten landen direkt auf dem Mount (`/downloads/.tmp_work`), nicht im RAM (`/tmp`).
  - `ffmpeg` ist ressourcenschonend auf 2 Threads limitiert.
  - Gunicorn läuft schlank mit 1 Worker und 4 Threads.

---

## Schnellstart auf DietPi / Raspberry Pi

1. **Docker installieren (falls noch nicht vorhanden):**
   ```bash
   sudo dietpi-software install 162 134
   ```

2. **Repository klonen:**
   ```bash
   git clone https://github.com/elgrande87/phantom-rail.git
   cd phantom-rail
   ```

3. **Container starten:**
   ```bash
   docker compose up -d --build
   ```

4. **Web-UI öffnen:**
   ```text
   http://<IP-DEINES-GERÄTS>:5000
   ```

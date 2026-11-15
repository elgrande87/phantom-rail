<p align="center">
  <img src="static/pasted.svg" alt="phantom-rail Logo" width="160">
</p>

# 👻🚂 phantom-rail

> Web-Oberfläche & Pipeline zum Herunterladen, Taggen und Binden von Hörbüchern als `.m4b` mit Kapiteln (speziell optimiert für Single-Board-Computer wie **Radxa Zero 3W**, DietPi & Raspberry Pi).

---

## 🌟 Highlights & Features

- 🔍 **Metadaten- & Audible-Abgleich:** Automatisches Suchen und Übernehmen von Buchtiteln, Autoren, Sprechern, Cover-Artworks und echten Kapitelstrukturen via Audnexus & Audible.
- ✂️ **Intelligenter Batch-Editor:** Schnelles Bereinigen, Suchen/Ersetzen und Nummerieren von Kapiteln mit einem Klick.
- 📁 **SSD-Ordnerverwaltung direkt in der UI:** Zielordner auf externen Medien (z. B. USB-3.0-SSD unter `/mnt/ssd/audiobooks`) über visuelle Reiter auswählen oder direkt neue Unterordner anlegen.
- 📱 **Mobile & iOS-optimiert:** Vollständig für iPhone/iPad und mobile Browser angepasst (Touch-freundliche Reiter, automatisches Verhindern von Input-Zoom, Safe-Area-Support).
- 🔄 **Hintergrund-Jobs & Reconnect:** Downloads und M4B-Konvertierung laufen serverseitig in Hintergrund-Threads weiter – auch wenn das Smartphone ins Standby geht oder die Seite neu geladen wird.
- ⚡ **Auto-Update-Check für `yt-dlp`:** Integrierter Status-Badge im Header prüft `yt-dlp`-Versionen und führt bei Bedarf ein In-Container-Update durch.
- 🪶 **Low-RAM & SBC-Optimierung (1 GB RAM):**
  - Temporäre Arbeitsdateien werden direkt auf den persistenten Speicher geleitet (`/downloads/.tmp_work`), um das RAM (`/tmp` / `tmpfs`) zu schonen.
  - `ffmpeg` arbeitet ressourcenschonend mit maximal 2 CPU-Threads.
  - Gunicorn ist stabil mit 1 Worker und 4 Threads konfiguriert.

---

## 🚀 Schnellstart

### 1. Voraussetzungen (z. B. DietPi / Raspberry Pi OS / Debian)

Docker & Compose installieren (auf DietPi z. B. via DietPi-Software):
```bash
sudo dietpi-software install 162 134
```

### 2. Repository klonen
```bash
git clone https://github.com/elgrande87/phantom-rail.git
cd phantom-rail
```

### 3. Docker-Mounts anpassen (`docker-compose.yml`)
Passe in `docker-compose.yml` den Pfad zu deiner externen SSD an (Standard: `/mnt/ssd/audiobooks`):

```yaml
services:
  phantom-rail:
    build: .
    container_name: phantom-rail
    restart: unless-stopped
    ports:
      - "5000:5000"
    volumes:
      - /mnt/ssd/audiobooks:/downloads
```

### 4. Container starten
```bash
docker compose up -d --build
```

### 5. Web-Oberfläche aufrufen
Öffne im Browser deines Laptops, Tablets oder Smartphones:
```text
http://<IP-DEINES-GERÄTS>:5000
```

---

## 🛠️ Wartung & Aktualisierung

Um die Anwendung auf den neuesten Stand aus dem `main`-Branch zu bringen:

```bash
cd ~/phantom-rail
git pull origin main
docker compose up -d --build
```

---

## 📂 Ordnerstruktur

```text
phantom-rail/
├── app.py               # Flask-Backend & SSE-Eventstream für Downloads
├── system_checks.py     # Versionsprüfung & Update-Logik für yt-dlp
├── templates/
│   └── index.html       # Web-Oberfläche (Dark Mode, responsive & iOS-optimiert)
├── static/
│   ├── folders.js       # Dynamische Ordnerauswahl & Reiter-Verwaltung
│   └── pasted.svg       # phantom-rail Logo
├── Dockerfile           # Python 3.11 Slim Image mit ffmpeg
└── docker-compose.yml   # Container-Konfiguration mit SSD-Mount
```

---

## 📄 Lizenz
MIT License.

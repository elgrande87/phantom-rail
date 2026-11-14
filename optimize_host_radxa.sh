#!/usr/bin/env bash
# ==============================================================================
# Host-Tuning-Skript fuer Radxa Zero 3W (RK3566, 1 GB RAM) & DietPi / Armbian
# ==============================================================================
set -euo pipefail

echo "=== phantom-rail Host-Optimierung fuer Radxa Zero 3W ==="

if [[ $EUID -ne 0 ]]; then
   echo "[ERROR] Dieses Skript muss mit Root-Rechten laufen: sudo bash $0"
   exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ------------------------------------------------------------------------------
# 1. WLAN Power Saving abschalten (Radxa Zero 3W AIC8800 WLAN bricht sonst ein)
# ------------------------------------------------------------------------------
WLAN_IF=$(iw dev 2>/dev/null | awk '$1=="Interface"{print $2}' | head -n 1 || true)
if [[ -n "$WLAN_IF" ]]; then
    echo "[1/3] Deaktiviere Power-Management auf $WLAN_IF ..."
    iw dev "$WLAN_IF" set power_save off || true

    # Dauerhaft via udev-Regel (funktioniert unter DietPi, Armbian & NetworkManager)
    cat <<EOF > /etc/udev/rules.d/70-wifi-powersave.rules
ACTION=="add", SUBSYSTEM=="net", KERNEL=="wlan*", RUN+="/usr/sbin/iw dev %k set power_save off"
EOF

    # Zusaetzlich via if-up.d falls vorhanden
    if [[ -d /etc/network/if-up.d ]]; then
        cat <<EOF > /etc/network/if-up.d/wlan-powersave-off
#!/bin/sh
[ "\$IFACE" = "$WLAN_IF" ] && iw dev \$IFACE set power_save off
EOF
        chmod +x /etc/network/if-up.d/wlan-powersave-off 2>/dev/null || true
    fi
else
    echo "[1/3] Kein WLAN-Interface via 'iw' gefunden (eventuell Ethernet/USB aktiv)."
fi

# ------------------------------------------------------------------------------
# 2. USB 3.0 SSD automatisch erkennen + 2-Stufen-Swap (zram + SSD-Swap)
# ------------------------------------------------------------------------------
echo "[2/3] Pruefe USB-SSD Mountpoint & richte 2-Stufen-Swap ein..."

detect_ssd_mount() {
    # Prioritaet A: Expliziter Parameter oder /mnt/ssd (falls gemountet)
    if [[ -n "${1:-}" ]] && mountpoint -q "$1" 2>/dev/null; then
        echo "$1"
        return 0
    fi
    if mountpoint -q "/mnt/ssd" 2>/dev/null; then
        echo "/mnt/ssd"
        return 0
    fi

    # Prioritaet B: Aus docker-compose.yml das /downloads Host-Volume auslesen
    if [[ -f "$SCRIPT_DIR/docker-compose.yml" ]]; then
        local compose_path
        compose_path=$(awk -F: '/:\/downloads/{gsub(/^[ \t-]+|[ \t"]+/, "", $1); print $1; exit}' "$SCRIPT_DIR/docker-compose.yml" || true)
        if [[ -n "$compose_path" && "$compose_path" != "." && -d "$compose_path" ]]; then
            local mp
            mp=$(df -P "$compose_path" 2>/dev/null | awk 'NR==2 {print $6}' || true)
            if [[ -n "$mp" && "$mp" != "/" && "$mp" != "/boot"* ]]; then
                echo "$mp"
                return 0
            fi
        fi
    fi

    # Prioritaet C: Kernel-Blockgeraete via lsblk nach USB-Laufwerken (TRAN=="usb") durchsuchen
    if command -v lsblk >/dev/null 2>&1; then
        local usb_mp
        usb_mp=$(lsblk -nrpo TRAN,MOUNTPOINT 2>/dev/null | awk '$1=="usb" && $2!="" && $2!="/" {print $2; exit}' || true)
        if [[ -z "$usb_mp" ]]; then
            # Fallback: Partitionen einer USB-Disk pruefen (Eltern-Device hat TRAN=usb)
            local usb_disk
            usb_disk=$(lsblk -ndo NAME,TRAN 2>/dev/null | awk '$2=="usb" {print $1; exit}' || true)
            if [[ -n "$usb_disk" ]]; then
                usb_mp=$(lsblk -nrpo MOUNTPOINT "/dev/$usb_disk" 2>/dev/null | awk '$1!="" && $1!="/" {print $1; exit}' || true)
            fi
        fi
        if [[ -n "$usb_mp" ]]; then
            echo "$usb_mp"
            return 0
        fi
    fi

    # Prioritaet D: Typische Mount-Verzeichnisse nach eingehaengten Laufwerken absuchen
    local candidate
    for candidate in /mnt/* /media/* /run/media/*/*; do
        if [[ -d "$candidate" ]] && mountpoint -q "$candidate" 2>/dev/null; then
            echo "$candidate"
            return 0
        fi
    done

    return 1
}

SSD_MOUNT=$(detect_ssd_mount "${1:-}" || true)
SSD_SWAP_ACTIVE=0
ZRAM_ACTIVE=0

if [[ -n "$SSD_MOUNT" ]]; then
    echo " -> USB-SSD Mountpoint erkannt: $SSD_MOUNT"
    FSTYPE=$(df -PT "$SSD_MOUNT" 2>/dev/null | awk 'NR==2 {print $2}' || true)
    SWAPFILE="$SSD_MOUNT/.phantom_swapfile"
    SWAP_SIZE_MB=2048

    # Pruefen, ob Dateisystem Swapfiles unterstuetzt (ext4/xfs; exfat/ntfs koennen keine Linux-Swapfiles)
    if [[ "$FSTYPE" =~ ^(vfat|exfat|ntfs|fuseblk)$ ]]; then
        echo " -> [HINWEIS] Dateisystem auf $SSD_MOUNT ist '$FSTYPE' (unterstuetzt keine Linux-Swapfiles)."
        echo "              Fuer SSD-Swap wird ext4 empfohlen."
    else
        if ! swapon --show=NAME --noheadings 2>/dev/null | grep -qx "$SWAPFILE"; then
            if [[ ! -f "$SWAPFILE" ]]; then
                echo " -> Erstelle ${SWAP_SIZE_MB}MB Swap-Datei auf SSD ($SWAPFILE)..."
                if ! fallocate -l "${SWAP_SIZE_MB}M" "$SWAPFILE" 2>/dev/null; then
                    dd if=/dev/zero of="$SWAPFILE" bs=1M count="$SWAP_SIZE_MB" status=progress
                fi
                chmod 600 "$SWAPFILE"
                mkswap "$SWAPFILE" >/dev/null
            else
                chmod 600 "$SWAPFILE"
            fi
            echo " -> Aktiviere SSD-Swap mit Prioritaet 10 (Sicherheitsnetz hinter zram)..."
            swapon -p 10 "$SWAPFILE" || true
        else
            echo " -> SSD-Swap ($SWAPFILE) ist bereits aktiv."
        fi

        if swapon --show=NAME --noheadings 2>/dev/null | grep -qx "$SWAPFILE"; then
            SSD_SWAP_ACTIVE=1
            # Dauerhaft in /etc/fstab eintragen (nofail, falls SSD beim Booten mal fehlt)
            if ! grep -qF "$SWAPFILE" /etc/fstab 2>/dev/null; then
                echo " -> Trage SSD-Swap dauerhaft in /etc/fstab ein (mit nofail,pri=10)..."
                echo "$SWAPFILE none swap sw,pri=10,nofail 0 0" >> /etc/fstab
            fi

            # Alten SD-Karten-Swap (/var/swap unter DietPi) abschalten, um MicroSD zu schonen
            if swapon --show=NAME --noheadings 2>/dev/null | grep -qx "/var/swap"; then
                echo " -> Deaktiviere langsamen SD-Karten-Swap (/var/swap) zum Schutz der MicroSD..."
                swapoff /var/swap || true
                sed -i 's|^/var/swap|# /var/swap (deaktiviert durch phantom-rail, nutzt SSD-Swap)|g' /etc/fstab 2>/dev/null || true
            fi
        fi
    fi
else
    echo " -> [!] Konnte keinen gemounteten USB-SSD-Pfad finden (weder /mnt/ssd noch via lsblk)."
    echo "        Tipp: Du kannst den Pfad auch direkt uebergeben: sudo bash $0 /pfad/zur/ssd"
fi

# Pruefen, ob zram aktiv ist (Stufe 1 mit hoher Prioritaet 100 im RAM)
if swapon --show=NAME --noheadings 2>/dev/null | grep -q "zram"; then
    ZRAM_ACTIVE=1
    echo " -> [OK] Schneller zram-Swap im RAM ist aktiv."
fi

echo " -> Aktive Swap-Speicher:"
swapon --show || true

# ------------------------------------------------------------------------------
# 3. Kernel Virtual Memory (sysctl) passend zum erkannten Swap-Setup tunen
# ------------------------------------------------------------------------------
echo "[3/3] Optimiere Kernel-Speicherverwaltung (sysctl) fuer 1 GB RAM & USB 3.0 SSD..."

# Swappiness intelligent waehlen:
# - Mit zram: 60 (komprimiert inaktive Seiten fruehzeitig im schnellen RAM)
# - Nur SSD-Swap (ohne zram): 25 (nutzt SSD-Swap bei Bedarf, haelt USB-Bus fuer Downloads frei)
# - Weder zram noch SSD-Swap (nur SD-Karte): 10 (schont die MicroSD-Karte)
if [[ "$ZRAM_ACTIVE" -eq 1 ]]; then
    TARGET_SWAPPINESS=60
elif [[ "$SSD_SWAP_ACTIVE" -eq 1 ]]; then
    TARGET_SWAPPINESS=25
else
    TARGET_SWAPPINESS=10
fi

SYSCTL_CONF="/etc/sysctl.d/99-phantom-rail-sbc.conf"
cat <<EOF > "$SYSCTL_CONF"
# Optimiert fuer Radxa Zero 3W (1 GB RAM) + USB 3.0 SSD
vm.swappiness=$TARGET_SWAPPINESS
vm.vfs_cache_pressure=50
vm.dirty_background_ratio=5
vm.dirty_ratio=15
EOF
sysctl --system >/dev/null 2>&1 || sysctl -p "$SYSCTL_CONF" >/dev/null || true

echo " -> vm.swappiness=$TARGET_SWAPPINESS gesetzt (zram=$ZRAM_ACTIVE, ssd_swap=$SSD_SWAP_ACTIVE)."
echo "=== Optimierung erfolgreich abgeschlossen! ==="

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

# 1. WLAN Power Saving abschalten (Radxa Zero 3W AIC8800 WLAN bricht sonst massiv ein)
WLAN_IF=$(iw dev 2>/dev/null | awk '$1=="Interface"{print $2}' | head -n 1 || true)
if [[ -n "$WLAN_IF" ]]; then
    echo "[+] Deaktiviere Power-Management auf $WLAN_IF ..."
    iw dev "$WLAN_IF" set power_save off || true
    
    # Dauerhaft machen via udev/systemd
    cat <<EOF > /etc/network/if-up.d/wlan-powersave-off || true
#!/bin/sh
[ "\$IFACE" = "$WLAN_IF" ] && iw dev \$IFACE set power_save off
EOF
    chmod +x /etc/network/if-up.d/wlan-powersave-off 2>/dev/null || true
else
    echo "[!] Kein WLAN-Interface via 'iw' gefunden (eventuell Ethernet/USB-Tethering aktiv)."
fi

# 2. Kernel Virtual Memory fuer 1 GB RAM & USB 3.0 SSD tunen
echo "[+] Optimiere Kernel-Speicherverwaltung fuer 1 GB RAM & USB 3.0 SSD..."
SYSCTL_CONF="/etc/sysctl.d/99-phantom-rail-sbc.conf"
cat <<EOF > "$SYSCTL_CONF"
# Schnellere Freigabe von Caches und Vermeidung von I/O-Spikes bei USB 3.0
vm.swappiness=20
vm.vfs_cache_pressure=50
vm.dirty_background_ratio=5
vm.dirty_ratio=10
EOF
sysctl -p "$SYSCTL_CONF" >/dev/null || true

# 3. zram / Swap-Check
echo "[+] Pruefe Swap/zram..."
TOTAL_SWAP=$(free -m | awk '/Swap/{print $2}')
if [[ "$TOTAL_SWAP" -lt 256 ]]; then
    echo "[WARNUNG] Kaum Swap vorhanden (${TOTAL_SWAP}M). Auf dem Radxa Zero 3W (1 GB RAM)"
    echo "          wird zram dringend empfohlen! Installiere auf DietPi z. B. ueber:"
    echo "          sudo dietpi-software -> zram swap aktivieren."
else
    echo "[OK] Swap/zram vorhanden (${TOTAL_SWAP}M)."
fi

echo "=== Optimierung erfolgreich abgeschlossen! ==="

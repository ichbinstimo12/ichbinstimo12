#!/usr/bin/env bash
# Einmalige Einrichtung eines frischen Ubuntu-Servers (22.04/24.04) fuer die Trading-Firma.
# Aufruf als root:   bash vps_setup.sh [--https]
#   --https  oeffnet zusaetzlich Port 80/443 (fuer das Dashboard ueber HTTPS, siehe docs/deploy_vps.md)
# Macht: Updates + automatische Sicherheitsupdates, Docker, Firewall (nur SSH), fail2ban, Swap,
#        Benutzer "firm" (Docker-Rechte, gleicher SSH-Schluessel wie root), SSH nur noch per Schluessel.
set -euo pipefail

HTTPS=0
[[ "${1:-}" == "--https" ]] && HTTPS=1
[[ $EUID -eq 0 ]] || { echo "Bitte als root ausfuehren (sudo -i)."; exit 1; }

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get -y upgrade
apt-get install -y ca-certificates curl git ufw fail2ban unattended-upgrades docker.io docker-compose-v2
dpkg-reconfigure -f noninteractive unattended-upgrades
systemctl enable --now docker fail2ban

timedatectl set-timezone UTC || true
systemctl enable --now systemd-timesyncd 2>/dev/null || true    # genaue Uhr: wichtig fuer Kurs-Frische

# Swap (kleine VPS): verhindert Abstuerze bei Speicherspitzen waehrend der Forschung
if ! swapon --show | grep -q .; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# Benutzer fuer den Betrieb
if ! id firm >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" firm
fi
usermod -aG docker firm
if [[ -f /root/.ssh/authorized_keys ]]; then
  install -d -m 700 -o firm -g firm /home/firm/.ssh
  install -m 600 -o firm -g firm /root/.ssh/authorized_keys /home/firm/.ssh/authorized_keys
fi

# Firewall: nur SSH (und optional HTTPS). Das Dashboard (Port 8000) ist nie direkt offen.
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
if [[ $HTTPS -eq 1 ]]; then ufw allow 80/tcp; ufw allow 443/tcp; fi
ufw --force enable

# SSH: Passwort-Login aus, aber nur wenn ein Schluessel hinterlegt ist (sonst sperrst du dich aus)
if [[ -s /home/firm/.ssh/authorized_keys ]]; then
  cat > /etc/ssh/sshd_config.d/90-firm.conf <<'CONF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
CONF
  systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
else
  echo "WARNUNG: kein SSH-Schluessel gefunden - Passwort-Login bleibt aktiv. Bitte Schluessel einrichten."
fi

echo
echo "Fertig. Weiter als Benutzer firm:   ssh firm@<server-ip>"
echo "Dann: Code holen, .env anlegen, starten - siehe docs/deploy_vps.md (Schritt 3)."

#!/usr/bin/env bash
# One-time Pi bring-up for marsnet (doc 00 section 3). Idempotent: safe to re-run.
#
#   sudo MARSNET_PSK=... rover/deploy/setup_pi.sh            # from the repo root, on the Pi
#
# Does: join marsnet with static 192.168.50.20, Wi-Fi powersave off, avahi (rover.local),
# chrony to the GX10, and install + enable rover.service. Does NOT pip-install: that runs as
# the normal user (see rover/deploy/README.md), not as root.
set -euo pipefail

SSID=${SSID:-marsnet}
PI_IP=${PI_IP:-192.168.50.20}
GX10_IP=${GX10_IP:-192.168.50.10}
REPO=${REPO:-$(cd "$(dirname "$0")/../.." && pwd)}
RUN_USER=${RUN_USER:-${SUDO_USER:-$(id -un)}}
HERE="$REPO/rover/deploy"

if [[ $EUID -ne 0 ]]; then
  echo "run with sudo" >&2
  exit 1
fi
if [[ -z "${MARSNET_PSK:-}" ]] && ! nmcli -t -f NAME con show | grep -qx "$SSID"; then
  echo "MARSNET_PSK is required the first time (the profile doesn't exist yet)" >&2
  exit 1
fi

echo "== hostname"
if [[ "$(hostname)" != "rover" ]]; then
  hostnamectl set-hostname rover
  sed -i 's/^127\.0\.1\.1.*/127.0.1.1\trover/' /etc/hosts
fi

echo "== Wi-Fi profile $SSID -> $PI_IP/24 via $GX10_IP"
if ! nmcli -t -f NAME con show | grep -qx "$SSID"; then
  nmcli con add type wifi con-name "$SSID" ssid "$SSID" \
    wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$MARSNET_PSK"
elif [[ -n "${MARSNET_PSK:-}" ]]; then
  nmcli con mod "$SSID" wifi-sec.psk "$MARSNET_PSK"
fi
# powersave 2 = disable. The default (on) costs 100-300 ms latency spikes on the Pi's
# Wi-Fi, against a 0.5 s ack budget and a 1.0 s heartbeat watchdog.
nmcli con mod "$SSID" \
  ipv4.method manual ipv4.addresses "$PI_IP/24" ipv4.gateway "$GX10_IP" ipv4.dns "$GX10_IP" \
  ipv6.method disabled \
  connection.autoconnect yes connection.autoconnect-priority 100 \
  connection.autoconnect-retries 0 \
  802-11-wireless.powersave 2

echo "== packages (avahi, chrony)"
apt-get install -y --no-install-recommends avahi-daemon chrony >/dev/null
systemctl enable --now avahi-daemon >/dev/null

echo "== chrony -> $GX10_IP"
install -d /etc/chrony/conf.d
sed "s/192\.168\.50\.10/$GX10_IP/" "$HERE/chrony-rover.conf" > /etc/chrony/conf.d/marsnet.conf
# The stock Debian pool lines are unreachable on a LAN-only network; leave them, chrony
# just marks them unreachable, but the GX10 must be the one selected.
systemctl restart chrony

echo "== rover.service (user $RUN_USER, repo $REPO)"
sed -e "s|@USER@|$RUN_USER|g" -e "s|@REPO@|$REPO|g" "$HERE/rover.service" \
  > /etc/systemd/system/rover.service
[[ -f /etc/default/rover ]] || install -m 0644 "$HERE/rover.env.example" /etc/default/rover
for g in gpio i2c video; do
  getent group "$g" >/dev/null && usermod -aG "$g" "$RUN_USER"
done
systemctl daemon-reload
systemctl enable rover.service >/dev/null

echo "== bringing up $SSID"
nmcli con up "$SSID" || echo "!! $SSID not in range yet; it will autoconnect when it is"

cat <<EOF

Done. Next (as $RUN_USER, not root):
  cd $REPO && python3 -m venv --system-site-packages .venv   # picamera2 comes from apt
  .venv/bin/pip install -e shared/py -e "rover[pi]"
  sudo systemctl start rover && journalctl -u rover -f
  .venv/bin/python -m rover.linkcheck          # from the Pi, or the GX10
  chronyc tracking                              # "System time" offset < 0.05 s
EOF

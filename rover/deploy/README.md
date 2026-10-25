# Pi ↔ GX10 bring-up (P2)

The network is `marsnet`, **hosted by the GX10** (192.168.50.10, gateway + DHCP + NTP). The
Pi is a static 192.168.50.20. Ports and URLs are doc 00 §3, unchanged. The GX10 side is P4's;
the exact commands are in [docs/requests/2026-10-24-P2-to-P4-gx10-hotspot.md](../../docs/requests/2026-10-24-P2-to-P4-gx10-hotspot.md).

```
 GX10 192.168.50.10  (AP "marsnet", chrony server)          Pi 192.168.50.20
   brain :8000  ── WS client ───────────────────────────▶  rover :8765/rover   cmds, heartbeat 4 Hz
                ◀─────────────────────────────────────────                     ack/done/err, telemetry 5 Hz
   mapper :8200 ◀── HTTP POST /ingest/station|sample ─────  uploader (SD-card queue, retry ≤ 30 s)
   browser/any  ── HTTP GET ────────────────────────────▶  :8080 /snapshot.jpg /stream.mjpg
```
Images never go over the WS: a 1 MB frame would delay heartbeats and acks.

## 1. Flash and first boot
1. Raspberry Pi Imager → **Raspberry Pi OS (64-bit) Bookworm** (or Lite). In the imager
   settings: user (e.g. `rover`), SSH on, Wi-Fi left empty or set to a phone hotspot for
   internet during setup.
2. Boot, `ssh`, `sudo apt update && sudo apt full-upgrade -y`.
3. Enable I2C: `sudo raspi-config nonint do_i2c 0`. The CSI camera is on by default.
4. `git clone` this repo into your home directory.

## 2. Join marsnet, clock sync, service
```bash
cd ~/autonomous-rover
sudo MARSNET_PSK='<from config/.env>' rover/deploy/setup_pi.sh
```
This is idempotent. It sets hostname `rover`, a static `192.168.50.20/24` via `192.168.50.10`,
**Wi-Fi powersave off**, avahi (`rover.local`), chrony → GX10, and installs `rover.service`.

## 3. Python
```bash
python3 -m venv --system-site-packages .venv    # picamera2 is an apt package
.venv/bin/pip install -e shared/py -e "rover[pi]"
sudo systemctl start rover && journalctl -u rover -f     # look for "inventory: {...}"
```
Device modes go in `/etc/default/rover` (`ROVER_MOTORS=sim`, etc.). See `rover.env.example`.

## 4. Check the link
```bash
.venv/bin/python -m rover.linkcheck            # on the Pi or the GX10; exits 1 on any FAIL
chronyc tracking                                # offset < 0.05 s
```
`linkcheck` reports every port in doc 00 §3, telemetry schema, WS round-trip (p50/p95),
clock skew from telemetry timestamps, and mapper `/health`. Run it from the GX10 for the
brain's view of the rover.

## 5. Acceptance (P2 I4)
Mid-`drive`, run `sudo nmcli radio wifi off; sleep 10; sudo nmcli radio wifi on` on the Pi.
The motors stop within 1 s (watchdog), the brain's `rover_link` logs `rover link down` and
then `rover link up`, and `uplink_backlog` drains.

## Laptop (no Pi)
```bash
python rover/rover/main.py --motors sim --camera sim          # WS :8765 + :8080
python rover/rover/linkcheck.py --only pi --pi 127.0.0.1      # all "ok", exit 0
```
The file form works from a bare checkout. `python -m rover.linkcheck` needs `pip install -e rover`
(done on the Pi in step 3). Uploads go to `--mapper` (default `http://192.168.50.10:8200`) and
queue in `--store` (default `<repo>/data/rover`), so set `--mapper http://127.0.0.1:8200` for
a local `mocks/fake_mapper.py`.

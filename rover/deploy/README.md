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

## 6. One-picture test: Pi → GX10 → dashboard
Goal: the Pi takes one photo, the GX10 stores it, and the dashboard's **Keyframe panel**
shows it. Until `calibrate_camera.py` has run, a station uploads keyframes only (no BEV, so
nothing new in the 3D view yet). That needs `ROVER_ALLOW_UNCALIBRATED=1` in `/etc/default/rover`,
which is the `rover.env.example` default. Restart with `sudo systemctl restart rover` after editing.

**A. GX10, once**
```bash
# network: hotspot + chrony server, commands in docs/requests/2026-10-24-P2-to-P4-gx10-hotspot.md
cd ~/autonomous-rover && python3 -m venv .venv && . .venv/bin/activate
pip install -e shared/py -r gx10/brain/requirements.txt -r gx10/mapper/requirements.txt
curl -sSf https://install.spacetimedb.com | sh
spacetime start --listen-addr 0.0.0.0:3000                     # its own terminal; leave running
curl -X POST http://192.168.50.10:3000/v1/identity              # -> brain  {identity, token}
curl -X POST http://192.168.50.10:3000/v1/identity              # -> mapper {identity, token}
cp config/.env.example config/.env   # set STDB_TOKEN_BRAIN, STDB_TOKEN_MAPPER, STDB_IDENTITY_MAPPER
./scripts/publish_stdb.sh            # module + claim_writer (brain) + add_writer (mapper)
./scripts/gen_bindings.sh            # dashboard/src/module_bindings
```
Vision isn't needed: a station with zero proposals never calls it.

**B. Pi, once:** steps 1–3 above. Then check `journalctl -u rover -n 30`: the inventory line
shows `camera: picamera2` and `imu: mpu6050@0x68`.

**C. Dashboard** (laptop on marsnet, or the GX10's own browser)
```bash
cp dashboard/.env.example dashboard/.env && cd dashboard && npm install && npm run dev
# open http://localhost:5173
```

**D. Start the GX10 services**: two terminals, each starting from the repo root.
```bash
source scripts/_env.sh && cd gx10 && uvicorn mapper.app:app --host 0.0.0.0 --port 8200
source scripts/_env.sh && cd gx10 && uvicorn brain.app:app  --host 0.0.0.0 --port 8000
```
Use these two rather than `dev_up.sh`, which also starts vision and overhead.

**E. Check the link** (on the GX10)
```bash
python rover/rover/linkcheck.py --only pi       # all ok: offset < 50 ms, p95 < 250 ms
curl http://192.168.50.10:8200/health           # {"ok":true,"stdb":true}
```

**F. The test** (on the GX10)
```bash
curl -X POST http://192.168.50.10:8000/api/run/start \
     -H 'Content-Type: application/json' -d '{"notes":"one-picture test"}'
spacetime sql marsrover "SELECT run_id, status FROM run"      # copy the newest run_id -> R
python rover/rover/sendcmd.py --pi 192.168.50.20 start_run run_id=R -- \
       station station_id=s_0001 n_views=1
```
Expected: `ack`, then `done … 1 keyframe(s), 0 proposal(s), mapper.ok=True`. Then check:
- `ls data/runs/R/stations/s_0001/` shows `kf_000.jpg` and `station.json`.
- `curl -o kf.jpg http://192.168.50.10:8200/files/runs/R/stations/s_0001/kf_000.jpg` downloads the photo.
- The dashboard **Keyframe panel** shows it, captioned `k_0001 · s_0001 · heading 0°`. Click it for full resolution.

**Resilience drill:** stop the mapper (Ctrl+C), then run
`sendcmd.py --pi 192.168.50.20 station station_id=s_0002 n_views=1`.
- You get `done … mapper.ok=False`, telemetry shows `uplink_backlog: 1`, and the brain logs an `upload_retry` event.
- Restart the mapper and the photo arrives within 30 s. The dashboard updates by itself.

**G. If it fails**

| Symptom | Fix |
|---|---|
| `err E_INTERNAL … needs the camera` | The venv must be built with `--system-site-packages` (picamera2 comes from apt), or the CSI ribbon isn't seated. `libcamera-hello --list-cameras` should list it. |
| `err E_INTERNAL … calibrated_at` | `ROVER_ALLOW_UNCALIBRATED=1` is missing from `/etc/default/rover`, or the service wasn't restarted. |
| `err E_BAD_ARGS station before start_run` | Send `start_run run_id=R` first, in the same command. |
| `done` but nothing on the dashboard | Mapper `/health` shows `stdb:false`: check `STDB_TOKEN_MAPPER` / `STDB_IDENTITY_MAPPER` and re-run `publish_stdb.sh`. Otherwise `R` isn't the dashboard's run (RunPicker), or the bindings weren't regenerated. |
| linkcheck clock offset FAIL | Run `chronyc tracking` on the Pi, and check the GX10's chrony `allow 192.168.50.0/24`. |
| `! ws://…: no answer` | Check `systemctl status rover` on the Pi, and that `ping 192.168.50.20` works. |

## Laptop (no Pi)
```bash
python rover/rover/main.py --motors sim --camera sim          # WS :8765 + :8080
python rover/rover/linkcheck.py --only pi --pi 127.0.0.1      # all "ok", exit 0
```
The file form works from a bare checkout. `python -m rover.linkcheck` needs `pip install -e rover`
(done on the Pi in step 3). Uploads go to `--mapper` (default `http://192.168.50.10:8200`) and
queue in `--store` (default `<repo>/data/rover`), so set `--mapper http://127.0.0.1:8200` for
a local `mocks/fake_mapper.py`.

The one-picture test on a laptop, all real code apart from the hardware:
```bash
python mocks/fake_mapper.py --port 8201 --no-stdb
python rover/rover/main.py --motors sim --camera sim --allow-uncalibrated --mapper http://127.0.0.1:8201
python rover/rover/sendcmd.py --pi 127.0.0.1 start_run run_id=r_20261024_120000 -- station station_id=s_0001 n_views=1
```

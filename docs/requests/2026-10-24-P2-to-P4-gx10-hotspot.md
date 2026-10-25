# P2 → P4: the GX10 hosts `marsnet` (no travel router) and serves NTP

**From:** Shrujan (P2, rover runtime) · **To:** Rohan (P4, brain/integration lead) · **Date:** 2026-10-24
**Status:** open. Item 1 is a contract change (doc 00 §3, your file); item 2 closes U-16; item 3 updates U-9.

We've decided to run without a separate travel router: **the GX10 is the Wi-Fi access
point**. The Pi joins it over Wi-Fi, and the GX10's Ethernet stays as the internet uplink
for Grok.

## 1. Doc 00 §3: keep the subnet, drop the router row

If NetworkManager's hotspot runs in *shared* mode on `192.168.50.10/24`, **every IP, port,
URL and CORS origin in doc 00, `config/network.yaml`, `rover_link.py` and the Pi code stays
the same**. The only change is who the gateway is:

| Host | IP | Link | Notes |
|---|---|---|---|
| ~~Router~~ | ~~192.168.50.1~~ | | removed |
| GX10 | 192.168.50.10 | **Wi-Fi AP `marsnet` (gateway, DHCP, DNS)** + Ethernet uplink | NM shared mode NATs Ethernet → marsnet for Grok |
| Pi (rover) | 192.168.50.20 | Wi-Fi | static, set on the Pi (`rover/deploy/setup_pi.sh`) |
| Laptop | 192.168.50.30 | Wi-Fi | static on the laptop, or any DHCP lease (CORS also allows `localhost:5173`) |

The `router:` block in `config/network.yaml` becomes `gateway: 192.168.50.10`. Whether
that's a patch bump to `CONTRACT_VERSION` is your call, since no port, field or unit changes.

### Commands for the GX10 (run once)

Pre-checks: ⚠️ UNVERIFIED that the GX10's Wi-Fi card does AP mode under DGX OS.
```bash
nmcli device | grep wifi                         # name of the Wi-Fi interface, e.g. wlP9s9
iw list | sed -n '/Supported interface modes/,/^\s*$/p'   # must list "AP"
```

Create the hotspot:
```bash
WLAN=wlP9s9                      # from the pre-check
PSK=...                          # from config/.env (MARSNET_PSK), never committed
sudo nmcli con add type wifi ifname "$WLAN" con-name marsnet autoconnect yes ssid marsnet \
  802-11-wireless.mode ap 802-11-wireless.band a 802-11-wireless.channel 36 \
  ipv4.method shared ipv4.addresses 192.168.50.10/24 ipv6.method disabled \
  wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PSK"
sudo nmcli con up marsnet
```
- **5 GHz (`band a`)**: the venue's 2.4 GHz band will be packed (docs/05 R5). If the card
  refuses `a`, fall back to `band bg channel 6`.
- `ipv4.method shared` gives DHCP and DNS (dnsmasq) plus NAT to the Ethernet uplink. The Pi
  uses a static address inside the subnet, so no DHCP reservation is needed.
- Please add `MARSNET_PSK=` to `config/.env.example`.

## 2. Chrony server on the GX10 (closes U-16)

The Pi side is ready (`rover/deploy/chrony-rover.conf`: `server 192.168.50.10 iburst`).
The GX10 needs:
```bash
sudo apt install chrony        # if `apt policy chrony` shows it isn't installed
# /etc/chrony/conf.d/marsnet.conf
allow 192.168.50.0/24
local stratum 10               # keeps serving time when the uplink is down
sudo systemctl restart chrony
```
I check it from the Pi with `chronyc tracking` (offset < 50 ms, doc 00 §4). `python -m
rover.linkcheck` also measures skew from the telemetry timestamps.

## 3. docs/05 U-9

U-9 can be resolved as "no router: the GX10 hosts `marsnet`; internet only through its
Ethernet, needed only for Grok". R5's mitigation now reads "move the GX10 closer / 5 GHz" in
place of "move the router closer".

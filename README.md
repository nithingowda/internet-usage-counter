# Raspberry Pi Internet Usage Monitor

A separate mobile-friendly web app for tracking traffic on `eth0`.

## Install

1. Copy this folder to your Raspberry Pi.
2. Make sure Docker Engine and the Docker Compose plugin are installed.
3. From this folder run:

```bash
docker compose up -d --build
```

4. Open `http://RASPBERRY_PI_IP:8787` from a device on your home network.

## Useful commands

```bash
docker compose logs -f
docker compose restart
docker compose down
docker compose up -d --build
```

## Data persistence

SQLite data is stored in `./data/usage.sqlite3` on the Raspberry Pi and survives container rebuilds/restarts. Back up the `data` directory.

## How counters work

The app samples Linux `/sys/class/net/eth0/statistics/rx_bytes` and `tx_bytes` every 2 seconds. It maintains cumulative totals and reset baselines in SQLite. Download/upload reset buttons only move the relevant baseline; they do not delete monthly history or alter Linux counters. Monthly totals roll over automatically by calendar month in `Asia/Kolkata`.

## Important limitations

- This measures traffic crossing the Raspberry Pi's `eth0` interface, not all devices on your Asus router.
- The app must be running to sample interface counters. Traffic during an app/container outage cannot be reconstructed from Linux interface counters after the fact.
- Interface counters can reset when the Pi reboots; the app detects a lower counter and continues accumulating from the new value.
- The web UI has no login by default. Keep port 8787 on your trusted LAN/Tailscale network; do not expose it directly to the public internet. Add authentication or a reverse proxy before broader exposure.
- `network_mode: host` is used so the app can read the host's network interface counters. The app listens on port 8787.

## Speed units and graph
The dashboard displays both:
- Mbps = megabits per second (bytes/second × 8 ÷ 1,000,000)
- MB/s = megabytes per second (bytes/second ÷ 1,000,000)

The historical graph plots download and upload speed for the selected time range (5 minutes to 24 hours). Use the graph selector to switch between Mbps and MB/s. Timestamped samples are stored in SQLite and remain available after a page reload; newly collected history begins when the feature is deployed.

## Dashboard settings and graph

Open **Settings** in the dashboard to change the display name and choose a network interface (for example, `eth0` or `wlan0`). Settings and per-interface counters are saved in the SQLite database under `./data`, so switching interfaces keeps each interface's totals separate. Live speed cards show both Mbps and MB/s. The graph supports 5-minute, 10-minute, 30-minute, 1-hour, 3-hour, 6-hour, 12-hour, and 24-hour windows; samples persist in SQLite.

The container uses host networking and mounts `/sys` read-only so it can read Linux interface counters. These counters measure traffic on the selected Raspberry Pi interface, not total traffic for every device on the router unless that traffic passes through the Pi.


## Dashboard customization

Open **Settings** in the dashboard to customize the appearance:

- Change the primary/download, accent/upload, background, card, and text colours with colour pickers.
- Set the dashboard title alignment to left, centre, or right (centre is the default).
- Upload a PNG, JPG, WEBP, or SVG logo up to 500 KB and adjust its displayed width.
- Preview colour, title, and logo changes in the page before saving; use **Reset appearance** to restore the defaults.

Appearance preferences, including the uploaded logo, are stored in the existing SQLite settings table and persist across page reloads and container restarts. The existing network-interface and dashboard-name settings continue to work as before.


## Time-range usage and RX/TX settings

The dashboard stores timestamped RX/TX traffic deltas in SQLite so the speed graph can show historical data after a page reload. Select 5 minutes, 10 minutes, 30 minutes, 1 hour, 3 hours, 6 hours, 12 hours, or 24 hours. The chart API aggregates samples into at most about 800 points for efficient loading and retains seven days of short-interval samples. Historical graph data begins accumulating after this feature is deployed.

The Data usage selector shows totals since reset or over the same time windows. A daily usage table below it shows the last seven calendar dates. Since-reset totals come from persistent cumulative counters, so they are not limited by the seven-day graph-history retention.

Under Settings → Traffic direction mapping, choose which interface counter is treated as Download and which as Upload. Defaults are RX (received) for Download and TX (transmitted) for Upload; the settings allow swapping them when an interface is presented in the opposite direction.

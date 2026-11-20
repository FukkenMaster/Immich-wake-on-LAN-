# Immich Wake-on-LAN Helper

A small companion service for an on-demand [Immich](https://immich.app/) setup: when the Immich server is asleep/offline, requests to `immich.lan` show a friendly status page (or a 503 for API/app clients), optionally send a Wake-on-LAN (WoL) magic packet to wake the server, and automatically redirect/recover once Immich is back online.

This is designed to run alongside **Nginx Proxy Manager (NPM)** on a low-power always-on host.

## Overview

- **Immich host:** `192.168.0.116:2283` (can sleep, e.g. S3, when not in use)
- **Proxy / always-on host:** `192.168.0.106` (Docker + Dockge, DNS/NPM)
- **Domain:** `immich.lan` (proxied by NPM)
- **Wake helper:** this service, running on `.106:18080` (`network_mode: host`)

### How it works

1. NPM proxies `immich.lan` → `http://192.168.0.116:2283` as normal.
2. If the upstream is unreachable, NPM intercepts `502/503/504` and forwards the request to the wake helper at `/__wake/`.
3. The wake helper:
   - **Browser page loads** (HTML/navigation): serves an interactive status page with the Immich logo, current status, and a **global** auto-wake toggle.
   - **App/API requests** (e.g. Immich Android app, non-browser clients): auto-triggers a WoL packet (when global auto-wake is enabled) and returns `503` JSON so the client knows the server is unavailable and can retry.
4. The helper probes Immich health at `http://192.168.0.116:2283/api/server/ping` (short timeout) and only reports **online** when that check succeeds.
5. When the server becomes healthy, the browser status page auto-reloads/redirects into Immich.

### Wake behavior (global)

- **Auto-wake enabled (all clients)** — *default*: any browser visit or app/API request that hits while Immich is down triggers a WoL packet (rate-limited) and the server is woken automatically.
- **Auto-wake off (click-to-wake for everyone)**: no client auto-wakes the server. The status page shows a **“Wake server”** button (and **“Try again”** after timeout); only an explicit manual wake click sends a WoL packet. Once woken, all clients can connect normally.
- The toggle on the status page controls this **globally** (server-side), not per-browser. The setting is persisted in `data/settings.json` on the helper host.

WoL packets are rate-limited (cooldown) and the helper reports **“Unable to wake the server”** after a configurable timeout if Immich does not become healthy.

## Repository contents

| File | Purpose |
|---|---|
| `docker-compose.yml` | Dockge/Docker Compose stack for the wake helper (host networking, env config, persisted settings volume) |
| `Dockerfile` | Builds the helper image (`python:3.12-alpine` + Flask) |
| `app.py` | Wake helper service: health probing, WoL magic packet, global settings, status/wake API, browser vs API handling |
| `index.html` | Offline/status UI (logo, status, global auto-wake toggle, manual wake/retry, auto-redirect when online) |
| `NPM-SETUP.md` | Step-by-step Nginx Proxy Manager configuration for `immich.lan` |
| `.gitignore` | Excludes runtime `data/` and common local artifacts |

## Configuration

Configured via environment variables in `docker-compose.yml`:

| Variable | Default | Description |
|---|---|---|
| `TARGET_IP` | `192.168.0.116` | Immich server IP |
| `TARGET_PORT` | `2283` | Immich server port |
| `TARGET_MAC` | `3C:52:82:72:31:9B` | MAC address of the Immich host NIC to wake (WoL target) |
| `WAKE_TIMEOUT` | `180` | Seconds after first wake attempt before reporting “Unable to wake the server” |
| `WAKE_COOLDOWN` | `20` | Minimum seconds between WoL packets (rate limit) |
| `PROBE_TIMEOUT` | `2` | HTTP timeout (seconds) for the Immich health probe |
| `PORT` | `18080` | Port the helper listens on |
| `SETTINGS_PATH` | `/data/settings.json` | Path to persisted global settings (mounted volume) |

Health probe: `http://<TARGET_IP>:<TARGET_PORT>/api/server/ping` is considered healthy on HTTP 2xx.

## Deployment (on `.106`, Docker + Dockge)

1. Place this folder on the `.106` host (e.g. in your Dockge stacks directory, such as `/opt/stacks/immich-wake-helper`).
2. Start/build the stack:
   ```bash
   cd /opt/stacks/immich-wake-helper
   docker compose up -d --build
   ```
   (Or deploy it as a stack from Dockge.)
3. Verify:
   ```bash
   curl http://127.0.0.1:18080/__wake/status
   # -> {"auto_wake":true,"state":"up"} when Immich is reachable
   # -> {"auto_wake":true,"state":"down"} (or "waking"/"timeout") when not
   curl http://127.0.0.1:18080/__wake/settings
   # -> {"auto_wake":true}
   ```

The stack uses `network_mode: host` so it can broadcast WoL packets on the LAN and listen on `PORT` directly on the host.

## Nginx Proxy Manager configuration

Edit the existing proxy host for `immich.lan` in NPM (running on `.106`). See **[NPM-SETUP.md](NPM-SETUP.md)** for click-by-click UI steps. Summary:

- **Details:** Scheme `http`, Forward `192.168.0.116:2283`, **Websockets Support** ON.
- **Custom location:** `/__wake/` → `http://127.0.0.1:18080`
- **Advanced:**
  ```nginx
  proxy_intercept_errors on;
  error_page 502 503 504 /__wake/;

  proxy_connect_timeout 5s;
  proxy_read_timeout 600s;
  proxy_send_timeout 600s;
  client_max_body_size 50000M;
  ```

This intercepts upstream failures and routes them to the helper while leaving normal Immich traffic (including uploads/websockets) proxied directly when the server is up.

## Helper HTTP endpoints

All served under `/__wake/` (routed to the helper by NPM):

| Endpoint | Method | Description |
|---|---|---|
| `/__wake/` | GET | Interactive status page (browser) or, for non-browser/API clients, triggers an automatic wake (if enabled) and returns `503` JSON |
| `/__wake/status` | GET | Returns current state JSON: `{state: "up"|"down"|"waking"|"timeout", auto_wake: bool, seconds?: number}` (no wake trigger) |
| `/__wake/wake` | GET/POST | Triggers a wake. `?manual=1` (or `true`/`yes`) performs an explicit/manual wake (allowed even when global auto-wake is off); without it, acts as an automatic wake (honors global setting) |
| `/__wake/settings` | GET | Returns `{ "auto_wake": bool }` |
| `/__wake/settings` | POST | Sets global auto-wake: JSON body `{ "auto_wake": true|false }` |

## Notes & troubleshooting

- **WoL prerequisites:** the Immich host/NIC must support and allow WoL for the relevant sleep state (S3), and the correct MAC must be targeted. A quick manual test from `.106` (while the target is asleep):
  ```bash
  python3 -c "import socket,binascii;mac=binascii.unhexlify('3C528272319B');s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.setsockopt(socket.SOL_SOCKET,socket.SO_BROADCAST,1);s.sendto(b'\xff'*6+mac*16,('255.255.255.255',9))"
  ```
- **Auto-suspend/shutdown of the Immich host** is intentionally out of scope for this helper and handled separately on the Immich host.
- **Mobile/background clients:** with global auto-wake enabled, app/API requests that hit while the server is down will trigger a wake (by design, to observe wake frequency). Disable global auto-wake to require a manual “Wake server” click instead.
- **Logo:** the status page loads the Immich logo from `ui.immich.app`; clients need internet access to display the image (wake logic itself is fully local). Inline the SVG in `index.html` if you need a fully offline/LAN-only page.
- **Event logs (important events only):** the helper logs key events to container stdout and to a persisted, auto-rotated file `./data/wake-events.log` (mounted to `/data`, rotated daily, ~5 backups kept). Logged events include: `wol_sent` (actual WoL packet sent, with manual/auto, trigger, client IP, User-Agent), `wol_send_error`, `auto_wake_suppressed` (throttled, when auto-wake is off), `wake_timeout`, and `server_up` (including wake duration when applicable). Routine probes/requests are not logged to avoid noise.
  - View: `ssh root@192.168.0.106 "docker logs --tail 200 immich-wake-helper"` and/or `ssh root@192.168.0.106 "tail -n 200 /opt/stacks/immich-wake-helper/data/wake-events.log"`
- **Persisted data:** global auto-wake state lives in `./data/settings.json`; event logs in `./data/wake-events.log*` (mounted to `/data`).

## License

Private / personal use.

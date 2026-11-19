# NPM configuration for `immich.lan`

Edit the existing **Proxy Host** for `immich.lan` in Nginx Proxy Manager (running on `.106`, `network_mode: host`).

## 1) Details tab
- **Scheme:** `http`
- **Forward Hostname / IP:** `192.168.0.116`
- **Forward Port:** `2283`
- **Websockets Support:** ON
- Keep your existing SSL / Force SSL / access list settings unchanged.

## 2) Custom Locations tab
Click **Add Location**:
- **Location:** `/__wake/`
- **Scheme:** `http`
- **Forward Hostname / IP:** `127.0.0.1`
- **Forward Port:** `18080`
- Leave the location "gear"/custom field empty (no extra directives needed).

This routes the wake/status page and its API (`/__wake/`, `/__wake/status`, `/__wake/wake`) to the wake helper running on `.106:18080`.

## 3) Advanced tab (Custom Nginx Configuration)
Paste the following:

```nginx
proxy_intercept_errors on;
error_page 502 503 504 /__wake/;

proxy_connect_timeout 5s;
proxy_read_timeout 600s;
proxy_send_timeout 600s;
client_max_body_size 50000M;
```

**What this does:**
- When Immich at `.116:2283` is asleep/unreachable, NPM receives 502/503/504, intercepts it, and forwards the request to `/__wake/`.
- For browser page loads, the wake helper returns the interactive status page (HTTP 200) and the page handles auto-wake per your browser's toggle.
- For app/API requests (e.g. the Immich Android app), the wake helper auto-triggers a wake (rate-limited) and returns a 503 JSON response so the client knows the server is unavailable and can retry.
- `proxy_connect_timeout 5s` makes the "server down" detection quick so the wake page/response appears promptly.
- Large body size and long timeouts preserve normal Immich uploads/long requests when the server is up.

Save the proxy host.

## 4) How it behaves
- **Immich up:** proxied normally (uploads, websockets) directly to `192.168.0.116:2283`.
- **Immich down/asleep:** visitor sees the offline page (Immich logo, "Immich is currently offline"). By default **Auto-wake on visit** is enabled (per-browser, stored in `localStorage`) so a WoL packet is sent (rate-limited) and status shows "Server is coming online, please stand by..." until `/api/server/ping` responds, then the page auto-reloads into Immich.
- **Click-to-wake mode:** uncheck "Auto-wake on visit" on the page to prevent automatic wakes for that browser; use the "Wake server" button instead.
- **Timeout:** if not healthy after ~3 minutes, shows "Unable to wake the server" with a "Try again" button.

## Notes
- The helper must be running on `.106:18080` for the offline page to work.
- The logo loads from `https://ui.immich.app/...` so clients need internet to display the image (the wake logic itself is local). If you want it fully LAN-only, the SVG can be inlined into `index.html`.
- WoL only works if `.116` accepts it in S3 for MAC `3C:52:82:72:31:9B`. Quick manual test from `.106` (host):  
  `python3 -c "import socket,binascii;mac=binascii.unhexlify('3C528272319B');s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.setsockopt(socket.SOL_SOCKET,socket.SO_BROADCAST,1);s.sendto(b'\xff'*6+mac*16,('255.255.255.255',9))"`

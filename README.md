# Ortho Viewer

A lightweight viewer for large GeoTIFF and JP2 orthophotos. No GIS software required.

- **Desktop mode** — pan, zoom, measure distances and areas
- **Web mode** — tile server + Leaflet.js browser interface

![Ortho Viewer Desktop Application](examples/Screenshot_3.png)

---

## Download & Run (no installation needed)

1. Go to the [Releases page](../../releases)
2. Download `OrthoViewer-windows.zip`
3. Extract the zip
4. Run `OrthoViewer.exe` — optionally drag a `.tif` or `.jp2` file onto it

No Python, no GDAL, no conda required.

---

## Run from Source

Requires Python 3.11+ with GDAL installed via conda-forge.

```bash
conda create -n orthoviewer python=3.11
conda activate orthoviewer
conda install -c conda-forge gdal pyproj numpy
pip install PyQt6 fastapi "uvicorn[standard]" pillow
```

**Desktop app:**
```bash
python main.py                        # open file dialog
python main.py path/to/file.tif       # open directly
```

**Web server:**
```bash
python web_server.py
# then open http://localhost:8765/admin to upload a file and get its embed link
```

Optional flags: `--host 0.0.0.0 --port 8765`

---

## Run as a server / embed on a webpage

The web server supports multiple orthophotos at once: upload files through a
password-protected admin page, then embed any of them with an `<iframe>`.

**With Docker + Cloudflare Tunnel** (recommended — no open inbound ports, HTTPS handled by Cloudflare):

1. In the [Cloudflare Zero Trust dashboard](https://one.dash.cloudflare.com/) →
   **Networks → Tunnels → Create a tunnel** → choose **Docker** as the connector.
   Copy just the token from the install command it shows you
   (`cloudflared tunnel run --token <THIS PART>`).
2. On the same tunnel, add a **Public Hostname**: pick a subdomain on a
   domain your Cloudflare account manages (e.g. `ortho.yourdomain.com`),
   service type **HTTP**, URL `http://ortho-viewer:8765` (that's the Docker
   Compose service name — cloudflared reaches it over the internal network,
   not the internet).
3. On the server:

   ```bash
   cp .env.example .env
   nano .env   # set ADMIN_PASSWORD and CLOUDFLARE_TUNNEL_TOKEN

   docker compose up -d --build
   ```

4. Open `https://ortho.yourdomain.com/admin` to upload files and copy embed
   links — they'll already be `https://ortho.yourdomain.com/view?file=...`.

The `ortho-viewer` container only publishes to `127.0.0.1:8765` on the host
(see `docker-compose.yml`), so the tunnel is the only public path in — no
firewall/port-forwarding changes needed on the server itself.

```html
<iframe src="https://ortho.yourdomain.com/view?file=site1.tif"
        style="width:100%;height:600px;border:0;"></iframe>
```

Uploaded rasters persist in `./data` (mounted as a volume).

Admin credentials come from the `ADMIN_USER` / `ADMIN_PASSWORD` environment
variables in `.env`.

**Without a tunnel**, the compose file still works the same way, but you'd
need to publish port 8765 publicly and put a TLS-terminating reverse proxy
(Caddy, nginx, Traefik) in front of it if the embedding page is HTTPS —
browsers block mixed HTTP content in an HTTPS iframe.

**Without Docker**, the same server works directly:

```bash
ADMIN_PASSWORD=your-password python web_server.py --host 0.0.0.0 --port 8765
```

---

## Features

- Reads GeoTIFF and JP2 (JPEG 2000) via GDAL — including multi-gigabyte files
- Hardware-accelerated tile rendering with overview levels
- Auto contrast stretch (2nd–98th percentile)
- Measure distance and area tools (geodesic, using pyproj)
- Web tile server compatible with any Leaflet.js map

---

## Building from Source (Windows)

```bash
conda activate orthoviewer
pip install pyinstaller
pyinstaller ortho_viewer.spec
# output: dist/OrthoViewer/
```

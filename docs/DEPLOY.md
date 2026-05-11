# Production Deployment

Target host: `87.106.22.81`
Target domain: `deepintel.boniforce.de`
Target path: `/root/deepintel`
Reverse proxy: Traefik (already running on host)

## 1. DNS

Create an A record:

```
deepintel.boniforce.de.  A  87.106.22.81
```

Wait for propagation: `dig deepintel.boniforce.de +short` should return the IP.

## 2. Verify Traefik network name

On the server, find the existing Traefik network and update `docker-compose.yml`
if it isn't `web`:

```bash
docker network ls | grep -i traefik
# common values: web, traefik, traefik_proxy, traefik_default
```

If different, edit `docker-compose.yml`: replace every `web` (in `networks:` and
`traefik.docker.network=...`) with the actual name.

## 3. Verify the cert resolver name

```bash
docker inspect traefik 2>/dev/null | grep -i certresolver
# or check the Traefik static config
```

If your Traefik resolver isn't called `letsencrypt`, update
`traefik.http.routers.deepintel.tls.certresolver=...` in the compose file.

## 4. Deploy

```bash
ssh root@87.106.22.81

# First time:
mkdir -p /root/deepintel
cd /root/deepintel
git clone https://github.com/Caohung77/deepintel2.git .
git checkout v0.1.0

# Create .env with production keys:
cp .env.example .env
nano .env   # fill in OPENAI_API_KEY, GEMINI_API_KEY, TAVILY_API_KEY, SECTORBENCH_API_TOKEN

# Build + start:
docker compose up -d --build

# Watch first-boot logs (Chromium download, model warmup):
docker compose logs -f deepintel
# Ctrl-C when you see "Uvicorn server started on 0.0.0.0:8501"
```

## 5. Verify

```bash
# Inside the container, healthcheck endpoint:
docker compose exec deepintel wget -qO- http://localhost:8501/_stcore/health
# Expected: ok

# From the host, through Traefik (after DNS + TLS):
curl -sSI https://deepintel.boniforce.de | head -3
# Expected: HTTP/2 200
```

Open https://deepintel.boniforce.de in browser.

## 6. Update

```bash
cd /root/deepintel
git fetch origin
git checkout vX.Y.Z   # or: git pull
docker compose up -d --build
```

## 7. Operations

```bash
# Logs
docker compose logs -f deepintel
docker compose logs --tail 200 deepintel

# Restart
docker compose restart deepintel

# Stop
docker compose down

# Shell into running container
docker compose exec deepintel bash

# Inspect persistent volume (sanctions cache, job files)
docker compose exec deepintel ls -lh /data/cache
docker compose exec deepintel ls /data/jobs

# Reset caches (sanctions data, Tavily, Wikidata, sectorbench)
docker compose exec deepintel rm -rf /data/cache/*
docker compose restart deepintel
```

## Troubleshooting

**"Worker daemon not running" in UI** — entrypoint.sh starts worker before
streamlit; if you see this, `docker compose logs deepintel | tail -50` to see
why the worker crashed. Most common cause: missing API key in `.env`.

**404 from Traefik** — usually wrong Traefik network. `docker inspect deepintel2`
under `Networks:` must show the same network Traefik is attached to.

**TLS certificate pending** — wait 60s for Let's Encrypt challenge. If still
pending, check DNS first, then Traefik's ACME log.

**OOM kill on first run** — sanctions list parse loads ~70k entries; on a
small VPS, bump container memory to ≥1 GB.

**Chromium fails to launch** — Playwright base image bundles deps, so this
should not happen. If it does: `docker compose exec deepintel playwright install chromium --with-deps`.

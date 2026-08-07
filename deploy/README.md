# APEX Trader — Deployment

This directory holds everything needed to serve **apex-trader.live**. The same
codebase runs in two environments with **zero code changes** — only where
`cloudflared` runs differs.

| Environment | API runs on | Frontend served by | Public access |
|---|---|---|---|
| **Local (Windows PC)** | `python -m api.run` (`127.0.0.1:8080`) | `npm run dev`/`preview` (`:3000`) | Cloudflare Tunnel from your PC |
| **VPS (Ubuntu)** | systemd `apex-api` (`127.0.0.1:8080`) | Caddy (static `frontend/dist`) | Cloudflare Tunnel on the VPS |

Both paths put Cloudflare in front for SSL + DDoS protection. Migrating from
local → VPS means running `cloudflared` on the VPS instead of your PC; no app
code changes.

---

## How the request flow works

```
User -> https://apex-trader.live -> Cloudflare (SSL/CDN)
                                        |
                                   Cloudflare Tunnel (cloudflared)
                                        |
            /api/*  -> http://localhost:8080  (FastAPI control plane)
            /*      -> frontend (Caddy static, or Vite preview on :3000)
```

Because the frontend is served from the **same origin** as the API in
production, the React app uses **relative** API paths (`/api/...`). This is
controlled by `frontend/.env.production` (`VITE_API_URL=` empty). In dev,
`frontend/.env.development` points at `http://localhost:8080`.

---

## Local development (Windows, no VPS)

1. **Start the API** (terminal 1):
   ```
   pip install -r requirements.txt
   python -m api.run
   ```
   Binds to `127.0.0.1:8080` (default `APEX_API_HOST`).

2. **Serve the frontend** (terminal 2). For a production-like origin use a build
   + preview so relative paths apply:
   ```
   cd frontend
   npm install
   npm run build
   npm run preview      # serves dist/ on :3000
   ```
   (For hot-reload dev work, `npm run dev` also works — it uses
   `.env.development` → `http://localhost:8080`.)

3. **Run the tunnel** (terminal 3) — install
   [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/),
   then:
   ```
   cloudflared tunnel login
   cloudflared tunnel create apex-trader
   cloudflared tunnel route dns apex-trader apex-trader.live
   ```
   Edit `deploy/cloudflared-config.yml` — replace `REPLACE_WITH_TUNNEL_ID` and
   `REPLACE_WITH_CREDENTIALS_PATH` (on Windows the credentials JSON is under
   `%USERPROFILE%\.cloudflared\<tunnel-id>.json`), then:
   ```
   cloudflared tunnel --config deploy/cloudflared-config.yml run apex-trader
   ```

4. Open **https://apex-trader.live**. The first user to register becomes admin.

> ⚠️ With the tunnel-only setup the platform is up only while your PC, the API,
> the frontend server, and `cloudflared` are all running. For real users, use a
> VPS so trading instances stay alive 24/7.

---

## VPS production (Ubuntu 22.04+)

1. SSH in as root and run the bootstrap:
   ```
   git clone https://github.com/Eugine336/apex-trader.git /opt/apex-trader
   bash /opt/apex-trader/deploy/setup-vps.sh
   ```
   This creates the `apex` user, installs deps, builds the frontend, and
   installs the `apex-api` + `cloudflared` systemd units.

2. **Reverse proxy** — install [Caddy](https://caddyserver.com/docs/install) and
   use `deploy/Caddyfile`. It serves `frontend/dist` and proxies `/api/*` to
   `localhost:8080`. Place your Cloudflare **origin certificate** at
   `/etc/ssl/apex-trader/cert.pem` and `key.pem` (see SSL below).

   Alternatively, run tunnel-only and point `cloudflared-config.yml`'s catch-all
   ingress at the static frontend instead of Caddy.

3. **Tunnel** — install `cloudflared`, then:
   ```
   cloudflared tunnel login
   cloudflared tunnel create apex-trader
   cloudflared tunnel route dns apex-trader apex-trader.live
   ```
   Fill in `deploy/cloudflared-config.yml` (tunnel ID + credentials path, e.g.
   `/etc/cloudflared/<tunnel-id>.json`).

4. **Start services**:
   ```
   systemctl start apex-api
   systemctl start cloudflared
   systemctl status apex-api
   ```

5. Open **https://apex-trader.live** and register the first (admin) user.

---

## Updating a running deployment

```
bash /opt/apex-trader/deploy/deploy.sh
```

Pulls latest, updates Python deps, rebuilds the frontend, and restarts
`apex-api`. (Restart `cloudflared` only if you changed its config.)

---

## Cloudflare DNS

You only need **one** record for the public hostname:

- **Tunnel (recommended, no public IP needed):** a `CNAME`
  `apex-trader.live -> <tunnel-id>.cfargotunnel.com`. Running
  `cloudflared tunnel route dns apex-trader apex-trader.live` creates this for
  you. Works from a home PC behind NAT.
- **Direct to VPS (no tunnel):** an `A` record
  `apex-trader.live -> <your VPS public IP>`, proxied (orange cloud) through
  Cloudflare. Requires opening port 443 on the VPS.

Add a `www` `CNAME -> apex-trader.live` too if you want the `www.` host (already
in the API CORS allowlist).

---

## SSL — Cloudflare "Full (Strict)"

- Cloudflare terminates public TLS at its edge (free certificate for the
  domain).
- **Tunnel:** the edge↔origin hop is the encrypted tunnel itself, so no origin
  cert is needed. Set SSL/TLS mode to **Full** (or Full (Strict)).
- **Direct-to-VPS with Caddy:** generate a Cloudflare **Origin Certificate**
  (Cloudflare dashboard → SSL/TLS → Origin Server), save the cert/key to
  `/etc/ssl/apex-trader/cert.pem` and `key.pem` (referenced by `Caddyfile`), and
  set SSL/TLS mode to **Full (Strict)** so Cloudflare validates the origin cert.

**Full (Strict)** means traffic is encrypted on both legs (user↔Cloudflare and
Cloudflare↔origin) and the origin certificate is verified — the most secure
mode.

---

## Environment variables (API)

| Var | Default | Purpose |
|---|---|---|
| `APEX_API_HOST` | `127.0.0.1` | Bind address. Keep loopback behind Cloudflare/Caddy. Set `0.0.0.0` only if exposing directly. |
| `APEX_API_PORT` | `8080` | API port. |
| `APEX_API_DATA_DIR` | `<repo>/api_data` | Auth DB, secrets, per-user instance state. |
| `APEX_API_CORS_ORIGINS` | localhost + `apex-trader.live` | Comma-separated allowed origins. |

The systemd unit (`apex-api.service`) sets these for the VPS.

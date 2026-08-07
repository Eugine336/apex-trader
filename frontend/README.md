# APEX Trader — Frontend Dashboard

A modern, dark-themed single-page dashboard for the APEX Trader multi-tenant
API. Users register, store their broker credentials, start/stop their isolated
trading instance, and monitor trades — all from the browser, no code access.

## Tech Stack

- **React 18** (functional components + hooks)
- **Vite** (dev server + build)
- **Tailwind CSS** (dark mode by default)
- **React Router v6**
- **Axios** (with JWT auth + refresh interceptor)
- **Recharts** (equity curve)

## Prerequisites

- Node.js 18+
- The APEX multi-tenant API running (default `http://localhost:8080`).
  Start it with `python -m api.run` from the repo root.

## Setup

```bash
cd frontend
npm install
cp .env.example .env   # adjust VITE_API_URL if your API is elsewhere
npm run dev
```

Open <http://localhost:3000>.

> The dev server runs on **port 3000** to match the API's default CORS origin
> (`http://localhost:3000`). If you change the port, also update
> `APEX_API_CORS_ORIGINS` on the backend.

## Configuration

| Variable       | Default                 | Description                |
| -------------- | ----------------------- | -------------------------- |
| `VITE_API_URL` | `http://localhost:8080` | Base URL of the API server |

## Build

```bash
npm run build     # outputs to dist/
npm run preview   # serve the production build locally
```

## User Flow

1. **Register / Login** — `/register` or `/login`.
2. **Broker Settings** (`/settings/broker`) — enter MT5 and/or Deriv credentials
   (stored encrypted server-side; never returned in plaintext).
3. **Trading Config** (`/settings/config`) — set risk, categories, symbols, etc.
4. **Instance Control** (`/settings/instance`) — Start / Stop / Restart and watch
   live logs.
5. **Dashboard** (`/`) — P&L, win rate, equity curve, recent trades.
6. **Trades** (`/trades`) — full filterable history.
7. **Positions** (`/positions`) — open positions, auto-refreshing while running.

## Project Structure

```
frontend/
├── index.html
├── package.json
├── vite.config.js          # dev server on :3000
├── tailwind.config.js
├── postcss.config.js
├── .env.example
└── src/
    ├── main.jsx            # entry — Router + AuthProvider
    ├── App.jsx             # routes
    ├── index.css           # Tailwind + scrollbar styles
    ├── api/                # axios client + service modules (match backend exactly)
    ├── context/            # AuthContext
    ├── components/         # Layout, tables, charts, dialogs, etc.
    ├── pages/              # Login, Register, Dashboard, Trades, ...
    └── utils/format.js     # money / percent / date formatting helpers
```

## Notes

- JWT access + refresh tokens are stored in `localStorage`
  (`apex_access_token`, `apex_refresh_token`). A response interceptor
  transparently refreshes an expired access token once before redirecting to
  `/login`.
- All API calls match the backend contract in `api/models.py` and `api/routes/`.
  `win_rate` from the API is a fraction (0–1) and is rendered as a percentage.
- This app is independent of the legacy single-user `dashboard/` package.

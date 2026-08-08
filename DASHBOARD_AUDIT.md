# APEX TRADER — Dashboard Data Audit (READ-FIRST)

Maps every frontend page → API endpoint → database query, and records what
data the trading engine produces, what reaches the frontend, and what is
missing. This is the basis for the Phase 2 implementation (real data panels
with admin/user separation).

All claims carry `file:line` references against the repository as read.

---

## 1. Data Separation Model (how user vs admin is enforced)

- **User-scoped data** is served by `api/routes/dashboard.py` (prefix
  `/api/dashboard`). Every endpoint depends on `get_current_user`
  (`api/auth.py:198`) and queries the DB with `int(user["id"])` — a user can
  only ever read their own trades/positions/stats.
- **Admin data** is served by `api/routes/admin.py` (prefix `/api/admin`),
  every endpoint gated by `get_current_admin` (`api/auth.py:233`) which 403s
  non-admins. Admin endpoints read across all users.
- The JWT carries only `sub` (user id); `is_admin` is re-read from the DB on
  every request (`api/auth.py:224`, `get_current_admin:237`). The frontend
  `is_admin` (from `/api/users/me`) is cosmetic; the backend is authoritative.

This separation is correct and is preserved by all Phase 2 additions.

---

## 2. The Single Source of Trade Data — `trade_history`

Schema (`api/database.py:66-80`):

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `user_id` | INTEGER | FK → users |
| `ticket` | TEXT | broker ticket |
| `symbol` | TEXT | |
| `direction` | TEXT | LONG/SHORT |
| `entry_price` | REAL | |
| `exit_price` | REAL | |
| `pnl` | REAL | realized P&L in account currency |
| `pnl_pips` | REAL | realized pips |
| `exit_reason` | TEXT | |
| `opened_at` | TEXT (nullable) | **was never populated** — see §6 |
| `closed_at` | TEXT | ISO timestamp |

Rows are written by **two paths**:
1. In-process API DB writes (`Database.insert_trade`, `api/database.py:322`).
2. The per-user trading subprocess via the reporting hook
   `api/trade_reporter.py:34` → `report_trade_close(...)`, called from
   `event_driven_bootstrap.py:6447` on every close. This is the live path in
   the multi-tenant deployment.

**Engine close path** (`event_driven_bootstrap._on_trade_closed`,
`event_driven_bootstrap.py:6149`) has access to: `symbol`, `direction`,
`pnl_dollars`, `pnl_pips`, `ticket`, `entry_price`, `exit_price`,
`exit_reason`. The entry context (`_entry_context`, `event_driven_bootstrap.py:5872`)
held `entry_price`, `sl`, `tp`, `risk_pips`, `exec_profile` — but **no entry
timestamp**, so `opened_at` was passed as `None`.

---

## 3. Frontend Pages — current data + gaps

### `frontend/src/pages/Dashboard.jsx` (user)
- **Fetches:** `getSummary()`, `getEquityCurve(500)`, `getHistory({limit:10})`
  (`Dashboard.jsx:27-31`).
- **Displays:** 4 cards — Total P&L, Win Rate (W/L sub), Total Trades, Instance
  status (`Dashboard.jsx:101-117`); Equity Curve chart (`:119-122`); Recent
  Trades table (`:124-135`).
- **Real vs placeholder:** all real.
- **Missing vs target:** Max Drawdown card; Daily P&L bar chart; open positions
  with live context on the dashboard itself.

### `frontend/src/pages/Trades.jsx` (user)
- **Fetches:** `getHistory({...filters})` (`Trades.jsx:24`). Filters: symbol,
  direction, since, until, paginated.
- **Displays:** filter bar + `TradesTable` + pagination. All real. Complete.

### `frontend/src/pages/Positions.jsx` (user)
- **Fetches:** `getPositions()` + `getStatus()` (`Positions.jsx:18`).
- **Displays:** `PositionsTable` (symbol, direction, lots, entry, sl, tp1, tp2,
  opened). Auto-refresh 5s when running. Real. Complete.
- **Note:** positions are read live (read-only) from the user's isolated
  `apex_positions.db` (`api/routes/dashboard.py:144`). No live unrealized P&L
  column (the engine DB row exposed has no current price).

### `frontend/src/pages/BrokerSettings.jsx`, `TradingConfig.jsx`, `InstanceControl.jsx`
- Forms / control surfaces. Not data dashboards. Complete and out of scope for
  this task except as navigation.

### `frontend/src/pages/AdminDashboard.jsx` (admin)
- **Fetches:** `getStats()`, `getInstances()` (`AdminDashboard.jsx:18`).
- **Displays:** 4 cards — Total Users, Active Instances, Trades Today, Total P&L
  (uptime in sub); Running Instances table; nav buttons (`:51-143`).
- **Missing vs target:** per-user performance table (PnL / trade count / win
  rate / last active); combined equity curve; trade volume over time; top
  performers ranking.

### `frontend/src/pages/AdminUsers.jsx`, `AdminInstances.jsx`, `AdminTrades.jsx`
- Management tables. Real and complete.

---

## 4. API — endpoints returning trading data

### User (scoped to JWT `user_id`)
| Endpoint | Returns | Backing query |
|---|---|---|
| `GET /api/dashboard/summary` (`dashboard.py:35`) | `DashboardSummary` {total_trades, total_pnl, win_rate, wins, losses, best_trade, worst_trade, instance_status, instance_alive} | `Database.trade_stats` (`database.py:380`) + `pm.instance_status` |
| `GET /api/dashboard/history` (`dashboard.py:56`) | `list[TradeResponse]` | `Database.list_trades` (`database.py:347`) |
| `GET /api/dashboard/equity-curve` (`dashboard.py:94`) | `list[EquityPoint{closed_at, equity}]` | `Database.equity_curve` (`database.py:402`) — cumulative realized P&L |
| `GET /api/dashboard/stats` (`dashboard.py:103`) | {total_trades, total_pnl, win_rate, best_trade, worst_trade, by_symbol} | `trade_stats` + per-symbol roll-up |
| `GET /api/dashboard/positions` (`dashboard.py:138`) | `list[PositionResponse]` | read-only of `apex_positions.db` |

### Admin (gated by `get_current_admin`)
| Endpoint | Returns | Backing query |
|---|---|---|
| `GET /api/admin/stats` (`admin.py:197`) | `AdminStatsResponse` | `list_users` + `global_trade_totals` + `count_trades_since` + `pm.active_count/system_uptime_seconds` |
| `GET /api/admin/users` / `/{id}` / PATCH | user rows | `list_users` / `get_user_by_id` / `set_user_*` |
| `GET /api/admin/instances` (`admin.py:98`) | `list[AdminInstanceResponse]` | `pm.all_statuses` + emails |
| `POST /api/admin/instances/{id}/start|stop` | `InstanceStatusResponse` | `pm.start_instance/stop_instance` |
| `GET /api/admin/trades` (`admin.py:168`) | `list[AdminTradeResponse]` | `Database.list_all_trades` (`database.py:418`) |

---

## 5. Data the engine/DB has but the frontend never shows (the gaps)

| Data | Available from | Had an endpoint? | Reached frontend? |
|---|---|---|---|
| Cumulative equity (per user) | `equity_curve` | ✅ | ✅ |
| Win rate / total P&L / counts (per user) | `trade_stats` | ✅ | ✅ |
| **Max drawdown (per user)** | derivable from cumulative P&L series | ❌ | ❌ |
| **Daily P&L (per user)** | `trade_history` grouped by `date(closed_at)` | ❌ | ❌ |
| **Avg trade duration** | `opened_at`/`closed_at` (opened_at was NULL) | ❌ | ❌ |
| Per-symbol win-rate | `/api/dashboard/stats` `by_symbol` | ✅ | ❌ (not rendered) |
| **Aggregate equity curve (all users)** | `trade_history` all rows | ❌ | ❌ |
| **Trade volume over time (all users)** | `trade_history` grouped by day | ❌ | ❌ |
| **Per-user performance / top performers** | `trade_history` grouped by user | ❌ | ❌ |
| Instance health (per user) | `pm.all_statuses` | ✅ | ✅ (AdminInstances) |

---

## 6. Phase 2 implementation decisions

To **enhance, not rebuild**, and avoid duplicate endpoints, Phase 2:

1. **Extends `DashboardSummary`** (`api/models.py`) with `max_drawdown` and
   `avg_duration_minutes` so the existing user-summary card row gains a real
   Max Drawdown card with no extra round-trip. Backed by new DB methods
   `Database.realized_drawdown` and `Database.avg_trade_duration_minutes`.
2. **Adds `GET /api/dashboard/daily-pnl`** → `list[DailyPnLPoint{date, pnl,
   trades}]`, backed by `Database.daily_pnl` (grouped by `date(closed_at)`).
   (Equity-curve already exists at `/api/dashboard/equity-curve`, so no
   `/api/trades/equity-curve` duplicate is created.)
3. **Adds `GET /api/admin/aggregate`** → `{equity_curve, daily_pnl}` across all
   users (`Database.global_equity_curve`, `Database.global_daily_pnl`).
4. **Adds `GET /api/admin/users/performance`** → per-user
   `{user_id, email, total_trades, total_pnl, win_rate, last_trade_at, status,
   alive}` (`Database.users_performance` + `pm` status), used for the per-user
   table and the top-performers ranking.
5. **Populates `opened_at`** at the source: stamp `entry_time` into
   `_entry_context` at fill (`event_driven_bootstrap.py:5872`) and pass it
   through `report_trade_close(..., opened_at=...)` (`:6447`). This makes
   average trade duration real **without a schema change** (`opened_at` already
   exists). Degrades gracefully (rows with NULL `opened_at` are skipped in the
   average).

Frontend Phase 2 adds:
- `DailyPnLChart.jsx` (recharts bar chart, green/red bars).
- User `Dashboard.jsx`: Max Drawdown card, Daily P&L chart, open positions panel.
- Admin `AdminDashboard.jsx`: aggregate equity curve, trade-volume bar chart,
  per-user performance table + top performers.
- API client functions in `frontend/src/api/dashboard.js` and `admin.js`.

No database schema changes. All new endpoints reuse the existing auth
dependencies (`get_current_user` / `get_current_admin`).

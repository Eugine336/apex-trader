import client from "./client";

// GET /api/dashboard/summary → DashboardSummary
export async function getSummary() {
  const { data } = await client.get("/api/dashboard/summary");
  return data;
}

// GET /api/dashboard/history → list[TradeResponse]
// params: { limit, offset, symbol, direction, since, until }
export async function getHistory(params = {}) {
  const clean = {};
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") clean[k] = v;
  }
  const { data } = await client.get("/api/dashboard/history", {
    params: clean,
  });
  return data;
}

// GET /api/dashboard/equity-curve → list[EquityPoint { closed_at, equity }]
export async function getEquityCurve(limit = 500) {
  const { data } = await client.get("/api/dashboard/equity-curve", {
    params: { limit },
  });
  return data;
}

// GET /api/dashboard/stats → { total_trades, total_pnl, win_rate, best_trade, worst_trade, by_symbol }
export async function getStats() {
  const { data } = await client.get("/api/dashboard/stats");
  return data;
}

// GET /api/dashboard/positions → list[PositionResponse]
export async function getPositions() {
  const { data } = await client.get("/api/dashboard/positions");
  return data;
}

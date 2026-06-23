import client from "./client";

// GET /api/config → TradingConfig
export async function getConfig() {
  const { data } = await client.get("/api/config");
  return data;
}

// PUT /api/config → { config, restart_required, message }
export async function updateConfig(payload) {
  const { data } = await client.put("/api/config", payload);
  return data;
}

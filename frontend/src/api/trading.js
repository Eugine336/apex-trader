import client from "./client";

// POST /api/trading/start → InstanceStatusResponse
export async function start() {
  const { data } = await client.post("/api/trading/start");
  return data;
}

// POST /api/trading/stop → InstanceStatusResponse
export async function stop() {
  const { data } = await client.post("/api/trading/stop");
  return data;
}

// POST /api/trading/restart → InstanceStatusResponse
export async function restart() {
  const { data } = await client.post("/api/trading/restart");
  return data;
}

// GET /api/trading/status → InstanceStatusResponse
export async function getStatus() {
  const { data } = await client.get("/api/trading/status");
  return data;
}

// GET /api/trading/logs?lines=N → InstanceLogResponse { lines: [] }
export async function getLogs(lines = 200) {
  const { data } = await client.get("/api/trading/logs", { params: { lines } });
  return data;
}

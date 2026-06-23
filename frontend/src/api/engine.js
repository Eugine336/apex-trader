import client from "./client";

// Live engine-state panels, proxied by the API to the authenticated user's
// own trading instance. All return the same JSON shapes the original
// single-user dashboard produced. A 503 means the user's instance is not
// running (or still starting) — callers should surface that as "start your
// instance" rather than a hard error.

// GET /api/engine/scanner
export async function getScanner() {
  const { data } = await client.get("/api/engine/scanner");
  return data;
}

// GET /api/engine/module-votes
export async function getModuleVotes() {
  const { data } = await client.get("/api/engine/module-votes");
  return data;
}

// GET /api/engine/ranker
export async function getRanker() {
  const { data } = await client.get("/api/engine/ranker");
  return data;
}

// GET /api/engine/decisions?limit=&decision_type=&symbol=
export async function getDecisions(params = {}) {
  const { data } = await client.get("/api/engine/decisions", { params });
  return data;
}

// GET /api/engine/decision-trace?limit=&symbol=
export async function getDecisionTrace(params = {}) {
  const { data } = await client.get("/api/engine/decision-trace", { params });
  return data;
}

// GET /api/engine/orchestrator?limit=&symbol=
export async function getOrchestrator(params = {}) {
  const { data } = await client.get("/api/engine/orchestrator", { params });
  return data;
}

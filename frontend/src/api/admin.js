import client from "./client";

// GET /api/admin/stats → AdminStatsResponse
export async function getStats() {
  const { data } = await client.get("/api/admin/stats");
  return data;
}

// GET /api/admin/users → list[UserResponse]
export async function getUsers() {
  const { data } = await client.get("/api/admin/users");
  return data;
}

// GET /api/admin/users/{id} → UserResponse
export async function getUser(userId) {
  const { data } = await client.get(`/api/admin/users/${userId}`);
  return data;
}

// PATCH /api/admin/users/{id} → UserResponse
export async function updateUser(userId, payload) {
  const { data } = await client.patch(`/api/admin/users/${userId}`, payload);
  return data;
}

// GET /api/admin/instances → list[AdminInstanceResponse]
export async function getInstances() {
  const { data } = await client.get("/api/admin/instances");
  return data;
}

// POST /api/admin/instances/{id}/start → InstanceStatusResponse
export async function startInstance(userId) {
  const { data } = await client.post(`/api/admin/instances/${userId}/start`);
  return data;
}

// POST /api/admin/instances/{id}/stop → InstanceStatusResponse
export async function stopInstance(userId) {
  const { data } = await client.post(`/api/admin/instances/${userId}/stop`);
  return data;
}

// GET /api/admin/trades?user_id=&limit=&offset= → list[AdminTradeResponse]
export async function getTrades({ userId, limit = 100, offset = 0 } = {}) {
  const params = { limit, offset };
  if (userId !== undefined && userId !== null && userId !== "") {
    params.user_id = userId;
  }
  const { data } = await client.get("/api/admin/trades", { params });
  return data;
}

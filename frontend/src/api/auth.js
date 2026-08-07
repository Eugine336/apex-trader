import client, { setTokens } from "./client";

// POST /api/auth/login → TokenPair {access_token, refresh_token, token_type}
export async function login(email, password) {
  const { data } = await client.post("/api/auth/login", { email, password });
  setTokens(data.access_token, data.refresh_token);
  return data;
}

// POST /api/auth/register → 201 TokenPair
export async function register(email, password) {
  const { data } = await client.post("/api/auth/register", { email, password });
  setTokens(data.access_token, data.refresh_token);
  return data;
}

// POST /api/auth/refresh → TokenPair
export async function refresh(refreshToken) {
  const { data } = await client.post("/api/auth/refresh", {
    refresh_token: refreshToken,
  });
  setTokens(data.access_token, data.refresh_token);
  return data;
}

// GET /api/users/me → UserResponse
export async function getMe() {
  const { data } = await client.get("/api/users/me");
  return data;
}

// POST /api/auth/password-reset/request → MessageResponse
export async function requestPasswordReset(email) {
  const { data } = await client.post("/api/auth/password-reset/request", {
    email,
  });
  return data;
}

// POST /api/auth/password-reset/confirm → MessageResponse
export async function confirmPasswordReset(token, newPassword) {
  const { data } = await client.post("/api/auth/password-reset/confirm", {
    token,
    new_password: newPassword,
  });
  return data;
}

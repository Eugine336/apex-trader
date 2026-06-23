import axios from "axios";

// Token storage keys (localStorage).
export const ACCESS_TOKEN_KEY = "apex_access_token";
export const REFRESH_TOKEN_KEY = "apex_refresh_token";

const BASE_URL = import.meta.env.VITE_API_URL || "http://localhost:8080";

export function getAccessToken() {
  return localStorage.getItem(ACCESS_TOKEN_KEY);
}

export function getRefreshToken() {
  return localStorage.getItem(REFRESH_TOKEN_KEY);
}

export function setTokens(accessToken, refreshToken) {
  if (accessToken) localStorage.setItem(ACCESS_TOKEN_KEY, accessToken);
  if (refreshToken) localStorage.setItem(REFRESH_TOKEN_KEY, refreshToken);
}

export function clearTokens() {
  localStorage.removeItem(ACCESS_TOKEN_KEY);
  localStorage.removeItem(REFRESH_TOKEN_KEY);
}

const client = axios.create({
  baseURL: BASE_URL,
  headers: { "Content-Type": "application/json" },
});

// Attach the bearer token to every request.
client.interceptors.request.use((config) => {
  const token = getAccessToken();
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// On 401, attempt a one-shot refresh, then replay the original request. If the
// refresh fails, clear tokens and bounce to the login page.
let refreshing = null;

client.interceptors.response.use(
  (response) => response,
  async (error) => {
    const original = error.config;
    const status = error.response?.status;

    // Never try to refresh the refresh call itself, and only retry once.
    const isRefreshCall = original?.url?.includes("/api/auth/refresh");
    if (status !== 401 || original?._retried || isRefreshCall) {
      return Promise.reject(error);
    }

    const refreshToken = getRefreshToken();
    if (!refreshToken) {
      clearTokens();
      redirectToLogin();
      return Promise.reject(error);
    }

    original._retried = true;
    try {
      if (!refreshing) {
        refreshing = axios
          .post(`${BASE_URL}/api/auth/refresh`, {
            refresh_token: refreshToken,
          })
          .then((res) => res.data)
          .finally(() => {
            // cleared after the in-flight refresh settles below
          });
      }
      const data = await refreshing;
      refreshing = null;
      setTokens(data.access_token, data.refresh_token);
      original.headers.Authorization = `Bearer ${data.access_token}`;
      return client(original);
    } catch (refreshError) {
      refreshing = null;
      clearTokens();
      redirectToLogin();
      return Promise.reject(refreshError);
    }
  }
);

function redirectToLogin() {
  if (window.location.pathname !== "/login") {
    window.location.assign("/login");
  }
}

// Normalize a backend error into a human-readable string. FastAPI returns
// either {detail: "..."} or {detail: [{msg, loc}, ...]} for validation errors.
export function extractError(error, fallback = "Something went wrong") {
  const detail = error?.response?.data?.detail;
  if (!detail) return error?.message || fallback;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => {
        const field = Array.isArray(d.loc) ? d.loc[d.loc.length - 1] : "";
        return field ? `${field}: ${d.msg}` : d.msg;
      })
      .join("; ");
  }
  return fallback;
}

export default client;

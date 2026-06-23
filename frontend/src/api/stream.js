import client, { getAccessToken } from "./client";

// Same base-URL resolution as the axios client: empty string (same-origin)
// behind Cloudflare, localhost in plain dev. ?? preserves an explicit "".
const BASE_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8080";

// Build the SSE URL. EventSource cannot set an Authorization header, so the
// access token is passed as a query parameter and validated server-side.
export function streamOverviewUrl(intervalSeconds = 2) {
  const token = getAccessToken();
  const qs = new URLSearchParams({
    token: token || "",
    interval: String(intervalSeconds),
  });
  return `${BASE_URL}/api/stream/overview?${qs.toString()}`;
}

// Polling fallback — one consolidated snapshot (JWT bearer via the axios client).
export async function getOverviewSnapshot() {
  const { data } = await client.get("/api/stream/snapshot");
  return data;
}

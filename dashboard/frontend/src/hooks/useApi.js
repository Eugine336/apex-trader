import { useState, useEffect, useCallback, useRef } from 'react';

const BASE = import.meta.env.VITE_API_URL || '';
const ENV_API_KEY = import.meta.env.VITE_DASHBOARD_API_KEY || '';
const LS_API_KEY = 'dd_dashboard_api_key';

// The dashboard API key. Prefer a runtime value saved in localStorage (set via
// the Controls page) so the key can be changed WITHOUT rebuilding the bundle;
// fall back to the build-time VITE_DASHBOARD_API_KEY if present.
export function getApiKey() {
  try {
    return localStorage.getItem(LS_API_KEY) || ENV_API_KEY;
  } catch {
    return ENV_API_KEY;
  }
}

export function setApiKey(key) {
  try {
    if (key) localStorage.setItem(LS_API_KEY, key);
    else localStorage.removeItem(LS_API_KEY);
  } catch {
    /* localStorage unavailable (private mode) — ignore */
  }
}

// When the backend has DD_DASHBOARD_API_KEY set, every /api/* request (not just
// mutating ones) must carry the matching X-API-Key header or it is rejected 401.
function authHeaders(extra = {}) {
  const key = getApiKey();
  return key ? { 'X-API-Key': key, ...extra } : { ...extra };
}

export function useApi(endpoint, interval = 5000) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const intervalRef = useRef(interval);

  const fetchData = useCallback(async () => {
    try {
      const res = await fetch(`${BASE}${endpoint}`, { headers: authHeaders() });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setData(await res.json());
      setError(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [endpoint]); // interval intentionally excluded — never changes per hook call

  useEffect(() => {
    fetchData();
    const ms = intervalRef.current;
    if (ms > 0) {
      const t = setInterval(fetchData, ms);
      return () => clearInterval(t);
    }
  }, [fetchData]); // stable: fetchData only changes if endpoint changes

  return { data, loading, error, refetch: fetchData };
}

export async function postControl(action, value = null) {
  const res = await fetch(`${BASE}/api/control`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ action, value }),
  });
  return res.json();
}

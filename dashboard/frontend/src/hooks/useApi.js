import { useState, useEffect, useCallback, useRef } from 'react';

const BASE = import.meta.env.VITE_API_URL || '';

export function useApi(endpoint, interval = 5000) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const intervalRef = useRef(interval);

  const fetchData = useCallback(async () => {
    try {
      const res = await fetch(`${BASE}${endpoint}`);
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
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, value }),
  });
  return res.json();
}

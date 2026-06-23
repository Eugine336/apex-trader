import { useCallback, useEffect, useRef, useState } from "react";

import { extractError } from "../api/client";

// Poll a live engine-state endpoint on an interval. Distinguishes the
// "instance not running" case (HTTP 503 from the proxy) from genuine errors so
// pages can show a friendly "start your instance" prompt instead of a red box.
//
// Real-time aware: polling auto-suspends while the browser tab is hidden and
// resumes (with an immediate refetch) the moment it regains focus, so
// backgrounded department pages never hammer the API.
//
// fetcher: async () => data  (one of the functions in api/engine.js)
// intervalMs: poll cadence (default 4s for a live feel)
export function useEnginePoll(fetcher, intervalMs = 4000) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [unavailable, setUnavailable] = useState(false);
  // Keep the latest fetcher without forcing the effect to re-subscribe.
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const load = useCallback(async () => {
    try {
      const result = await fetcherRef.current();
      setData(result);
      setUnavailable(false);
      setError("");
    } catch (err) {
      if (err?.response?.status === 503) {
        setUnavailable(true);
        setError("");
      } else {
        setError(extractError(err, "Failed to load engine data"));
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let timer = null;

    const start = () => {
      if (timer || intervalMs <= 0) return;
      timer = setInterval(load, intervalMs);
    };
    const stop = () => {
      if (timer) {
        clearInterval(timer);
        timer = null;
      }
    };

    const onVisibility = () => {
      if (document.hidden) {
        stop();
      } else {
        load();
        start();
      }
    };

    load();
    start();
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [load, intervalMs]);

  return { data, loading, error, unavailable, refetch: load };
}

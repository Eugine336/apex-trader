import { useCallback, useEffect, useRef, useState } from "react";

import { getOverviewSnapshot, streamOverviewUrl } from "../api/stream";

// Real-time overview feed with graceful degradation:
//
//   SSE (EventSource)  ──onerror──▶  polling fallback  ──reconnect──▶  SSE
//
// • Primary transport is a Server-Sent Events stream pushed by the control
//   plane every ~2s.
// • If the stream errors/drops (proxy hiccup, instance restart, token rotation)
//   we immediately switch to REST polling and keep retrying SSE with capped
//   exponential backoff.
// • When the tab is hidden we suspend everything and resume instantly on focus,
//   so a backgrounded dashboard never hammers the API.
//
// Returns { snapshot, connected, transport } where transport is
// "sse" | "poll" | "off".
const POLL_INTERVAL_MS = 3000;
const BACKOFF_START_MS = 2000;
const BACKOFF_MAX_MS = 30000;

export function useLiveStream({ enabled = true, intervalSeconds = 2 } = {}) {
  const [snapshot, setSnapshot] = useState(null);
  const [connected, setConnected] = useState(false);
  const [transport, setTransport] = useState("off");

  const esRef = useRef(null);
  const pollTimer = useRef(null);
  const reconnectTimer = useRef(null);
  const backoff = useRef(BACKOFF_START_MS);
  const stopped = useRef(false);

  const clearTimers = useCallback(() => {
    if (pollTimer.current) {
      clearInterval(pollTimer.current);
      pollTimer.current = null;
    }
    if (reconnectTimer.current) {
      clearTimeout(reconnectTimer.current);
      reconnectTimer.current = null;
    }
  }, []);

  const closeSSE = useCallback(() => {
    if (esRef.current) {
      esRef.current.close();
      esRef.current = null;
    }
  }, []);

  const startPolling = useCallback(() => {
    if (pollTimer.current || stopped.current) return;
    setTransport((t) => (t === "sse" ? t : "poll"));
    const tick = async () => {
      try {
        const snap = await getOverviewSnapshot();
        if (stopped.current) return;
        setSnapshot(snap);
        setConnected(true);
      } catch {
        if (!stopped.current) setConnected(false);
      }
    };
    tick();
    pollTimer.current = setInterval(tick, POLL_INTERVAL_MS);
  }, []);

  const connectSSE = useCallback(() => {
    if (stopped.current || typeof window === "undefined" || !window.EventSource) {
      // SSE unsupported — live on polling only.
      startPolling();
      return;
    }
    closeSSE();
    let es;
    try {
      es = new EventSource(streamOverviewUrl(intervalSeconds));
    } catch {
      startPolling();
      return;
    }
    esRef.current = es;

    es.onopen = () => {
      backoff.current = BACKOFF_START_MS;
      setConnected(true);
      setTransport("sse");
      // SSE is healthy — drop the polling fallback if it was running.
      if (pollTimer.current) {
        clearInterval(pollTimer.current);
        pollTimer.current = null;
      }
    };

    es.onmessage = (evt) => {
      if (stopped.current) return;
      try {
        setSnapshot(JSON.parse(evt.data));
        setConnected(true);
      } catch {
        // Ignore malformed frames; keep the last good snapshot.
      }
    };

    es.onerror = () => {
      // Stream dropped. Fall back to polling and retry SSE with backoff.
      closeSSE();
      if (stopped.current) return;
      setConnected(false);
      startPolling();
      if (!reconnectTimer.current) {
        reconnectTimer.current = setTimeout(() => {
          reconnectTimer.current = null;
          connectSSE();
        }, backoff.current);
        backoff.current = Math.min(backoff.current * 2, BACKOFF_MAX_MS);
      }
    };
  }, [closeSSE, intervalSeconds, startPolling]);

  const teardown = useCallback(() => {
    closeSSE();
    clearTimers();
    backoff.current = BACKOFF_START_MS;
  }, [clearTimers, closeSSE]);

  useEffect(() => {
    if (!enabled) {
      stopped.current = true;
      teardown();
      setTransport("off");
      setConnected(false);
      return undefined;
    }

    stopped.current = false;
    connectSSE();

    const onVisibility = () => {
      if (document.hidden) {
        teardown();
        setConnected(false);
        setTransport("off");
      } else {
        backoff.current = BACKOFF_START_MS;
        connectSSE();
      }
    };
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      stopped.current = true;
      document.removeEventListener("visibilitychange", onVisibility);
      teardown();
    };
  }, [enabled, connectSSE, teardown]);

  return { snapshot, connected, transport };
}

import { useEffect, useRef, useState, useCallback } from "react";

/**
 * Subscribes to the WebSocket and merges live pushes into a single state object.
 *
 * Handles two message types:
 *   "state_update"  → { status, open_trades, risk }       pushed every 2s
 *   "scanner_update" → { scanner, performance }            pushed every 5s
 *
 * Returns a merged object so consumers can read any key without caring which
 * message it came from.
 */
export default function useLiveState() {
  const [state, setState] = useState({});
  const ws = useRef(null);

  const connect = useCallback(() => {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    ws.current = new WebSocket(`${protocol}//${window.location.host}/ws`);

    ws.current.onmessage = (evt) => {
      try {
        const msg = JSON.parse(evt.data);
        if (msg.type === "state_update" || msg.type === "scanner_update") {
          // merge incoming keys into accumulated state
          const { type, ...payload } = msg;
          setState((prev) => ({ ...prev, ...payload }));
        }
      } catch {
        // ignore parse errors
      }
    };

    ws.current.onclose = () => {
      setTimeout(connect, 3000);
    };

    ws.current.onerror = () => {
      ws.current?.close();
    };
  }, []);

  useEffect(() => {
    connect();
    return () => ws.current?.close();
  }, [connect]);

  return state;
}

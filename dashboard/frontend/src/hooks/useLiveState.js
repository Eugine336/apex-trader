import { useEffect, useRef, useState, useCallback } from 'react';

const API_KEY = import.meta.env.VITE_DASHBOARD_API_KEY || '';

export default function useLiveState() {
  const [state, setState] = useState({});
  const [connected, setConnected] = useState(false);
  const [lastUpdate, setLastUpdate] = useState(null);
  const ws = useRef(null);

  const connect = useCallback(() => {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${protocol}//${window.location.host}/ws`;
    // Browsers can't set custom headers on a WebSocket, so the API key is
    // offered as a subprotocol — the backend reads it from Sec-WebSocket-Protocol.
    ws.current = API_KEY ? new WebSocket(url, [API_KEY]) : new WebSocket(url);

    ws.current.onopen = () => setConnected(true);

    ws.current.onmessage = (evt) => {
      try {
        const msg = JSON.parse(evt.data);
        if (msg.type === 'state_update' || msg.type === 'scanner_update') {
          const { type, ...payload } = msg;
          setState((prev) => ({ ...prev, ...payload }));
          setLastUpdate(Date.now());
        }
      } catch {}
    };

    ws.current.onclose = () => {
      setConnected(false);
      setTimeout(connect, 3000);
    };

    ws.current.onerror = () => ws.current?.close();
  }, []);

  useEffect(() => {
    connect();
    return () => ws.current?.close();
  }, [connect]);

  return { state, connected, lastUpdate };
}

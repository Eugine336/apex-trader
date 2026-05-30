import { useEffect, useRef, useState, useCallback } from 'react';

export default function useLiveState() {
  const [state, setState] = useState({});
  const [connected, setConnected] = useState(false);
  const [lastUpdate, setLastUpdate] = useState(null);
  const ws = useRef(null);

  const connect = useCallback(() => {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws.current = new WebSocket(`${protocol}//${window.location.host}/ws`);

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

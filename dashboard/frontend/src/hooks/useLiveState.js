import { useEffect, useRef, useState } from "react";

export default function useLiveState() {
  const [state, setState] = useState(null);
  const ws = useRef(null);

  useEffect(() => {
    const connect = () => {
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      ws.current = new WebSocket(`${protocol}//${window.location.host}/ws`);

      ws.current.onmessage = (evt) => {
        try {
          const msg = JSON.parse(evt.data);
          if (msg.type === "state_update") {
            setState(msg);
          }
        } catch {
          // ignore parse errors
        }
      };

      ws.current.onclose = () => {
        setTimeout(connect, 3000);
      };

      ws.current.onerror = () => {
        ws.current.close();
      };
    };
    connect();
    return () => ws.current?.close();
  }, []);

  return state;
}

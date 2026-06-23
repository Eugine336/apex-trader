import { createContext, useContext } from "react";

import { useLiveStream } from "../hooks/useLiveStream";

// Shares a single real-time overview feed across the whole authenticated app
// (topbar ticker + Command Center) so we open exactly one stream per session.
const RealtimeContext = createContext({
  snapshot: null,
  connected: false,
  transport: "off",
});

export function RealtimeProvider({ children, enabled = true }) {
  const live = useLiveStream({ enabled, intervalSeconds: 2 });
  return (
    <RealtimeContext.Provider value={live}>{children}</RealtimeContext.Provider>
  );
}

export function useRealtime() {
  return useContext(RealtimeContext);
}

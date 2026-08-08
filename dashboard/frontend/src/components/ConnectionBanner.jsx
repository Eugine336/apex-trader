import React from 'react';

export default function ConnectionBanner() {
  return (
    <div className="conn-banner">
      <div className="spinner" />
      <span>WebSocket disconnected — Reconnecting...</span>
    </div>
  );
}

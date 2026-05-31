import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import './styles/globals.css';

class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }
  static getDerivedStateFromError(error) {
    return { error: error.message || String(error) };
  }
  render() {
    if (this.state.error) {
      return (
        <div style={{
          position: 'fixed', inset: 0, background: '#070710',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontFamily: 'monospace', color: '#ef4444', padding: 40,
        }}>
          <div style={{ maxWidth: 640 }}>
            <div style={{ color: '#f59e0b', fontSize: 18, fontWeight: 700, marginBottom: 16 }}>
              ⚠ APEX TRADER — Dashboard Error
            </div>
            <pre style={{ background: '#111120', border: '1px solid #2a2a45', borderRadius: 6,
              padding: 20, fontSize: 13, color: '#ef4444', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
              {this.state.error}
            </pre>
            <div style={{ marginTop: 16, fontSize: 12, color: '#7878a0' }}>
              Open browser DevTools (F12) → Console for full stack trace.
            </div>
            <button onClick={() => window.location.reload()} style={{
              marginTop: 16, padding: '8px 20px', background: '#2563eb',
              color: '#fff', border: 'none', borderRadius: 4, cursor: 'pointer', fontSize: 13,
            }}>
              Reload
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

createRoot(document.getElementById('root')).render(
  <ErrorBoundary>
    <App />
  </ErrorBoundary>
);

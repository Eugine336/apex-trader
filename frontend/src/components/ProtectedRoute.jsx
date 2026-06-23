import { Navigate, useLocation } from "react-router-dom";

import { useAuth } from "../context/AuthContext";

// Gates protected routes. While the auth bootstrap is in flight we render a
// neutral splash so we don't flash the login page for already-authed users.
export default function ProtectedRoute({ children }) {
  const { isAuthenticated, loading } = useAuth();
  const location = useLocation();

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center bg-gray-900 text-gray-400">
        <div className="animate-pulse text-sm">Loading…</div>
      </div>
    );
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" replace state={{ from: location }} />;
  }

  return children;
}

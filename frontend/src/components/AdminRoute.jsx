import { Navigate } from "react-router-dom";

import { useAuth } from "../context/AuthContext";

// Gates admin-only routes. Assumes it renders inside ProtectedRoute (so the
// user is already authenticated); redirects non-admins back to the dashboard.
export default function AdminRoute({ children }) {
  const { user, loading } = useAuth();

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center bg-gray-900 text-gray-400">
        <div className="animate-pulse text-sm">Loading…</div>
      </div>
    );
  }

  if (!user?.is_admin) {
    return <Navigate to="/" replace />;
  }

  return children;
}

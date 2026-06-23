import { Navigate, Route, Routes } from "react-router-dom";

import AdminRoute from "./components/AdminRoute";
import Layout from "./components/Layout";
import ProtectedRoute from "./components/ProtectedRoute";
import AdminDashboard from "./pages/AdminDashboard";
import AdminInstances from "./pages/AdminInstances";
import AdminTrades from "./pages/AdminTrades";
import AdminUsers from "./pages/AdminUsers";
import BrokerSettings from "./pages/BrokerSettings";
import Dashboard from "./pages/Dashboard";
import InstanceControl from "./pages/InstanceControl";
import Login from "./pages/Login";
import Positions from "./pages/Positions";
import Register from "./pages/Register";
import Trades from "./pages/Trades";
import TradingConfig from "./pages/TradingConfig";

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/register" element={<Register />} />

      <Route
        element={
          <ProtectedRoute>
            <Layout />
          </ProtectedRoute>
        }
      >
        <Route path="/" element={<Dashboard />} />
        <Route path="/trades" element={<Trades />} />
        <Route path="/positions" element={<Positions />} />
        <Route path="/settings/broker" element={<BrokerSettings />} />
        <Route path="/settings/config" element={<TradingConfig />} />
        <Route path="/settings/instance" element={<InstanceControl />} />

        <Route
          path="/admin"
          element={
            <AdminRoute>
              <AdminDashboard />
            </AdminRoute>
          }
        />
        <Route
          path="/admin/users"
          element={
            <AdminRoute>
              <AdminUsers />
            </AdminRoute>
          }
        />
        <Route
          path="/admin/instances"
          element={
            <AdminRoute>
              <AdminInstances />
            </AdminRoute>
          }
        />
        <Route
          path="/admin/trades"
          element={
            <AdminRoute>
              <AdminTrades />
            </AdminRoute>
          }
        />
      </Route>

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

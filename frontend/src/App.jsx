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
import ActiveTrades from "./pages/engine/ActiveTrades";
import Decisions from "./pages/engine/Decisions";
import DecisionTrace from "./pages/engine/DecisionTrace";
import Governor from "./pages/engine/Governor";
import ModuleVotes from "./pages/engine/ModuleVotes";
import Operations from "./pages/engine/Operations";
import Orchestrator from "./pages/engine/Orchestrator";
import Planner from "./pages/engine/Planner";
import PositionHealth from "./pages/engine/PositionHealth";
import Ranker from "./pages/engine/Ranker";
import RiskMonitor from "./pages/engine/RiskMonitor";
import Scanner from "./pages/engine/Scanner";
import TradeHistory from "./pages/engine/TradeHistory";
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

        <Route path="/engine/scanner" element={<Scanner />} />
        <Route path="/engine/votes" element={<ModuleVotes />} />
        <Route path="/engine/ranker" element={<Ranker />} />
        <Route path="/engine/decisions" element={<Decisions />} />
        <Route path="/engine/trace" element={<DecisionTrace />} />
        <Route path="/engine/orchestrator" element={<Orchestrator />} />

        <Route path="/engine/risk" element={<RiskMonitor />} />
        <Route path="/engine/governor" element={<Governor />} />
        <Route path="/engine/planner" element={<Planner />} />
        <Route path="/engine/operations" element={<Operations />} />
        <Route path="/engine/active-trades" element={<ActiveTrades />} />
        <Route path="/engine/position-health" element={<PositionHealth />} />
        <Route path="/engine/history" element={<TradeHistory />} />

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

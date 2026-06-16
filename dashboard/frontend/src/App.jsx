import React from 'react';
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import Layout from './components/Layout';
import Overview from './pages/Overview';
import ActiveTrades from './pages/ActiveTrades';
import TradeHistory from './pages/TradeHistory';
import Scanner from './pages/Scanner';
import ModuleVotes from './pages/ModuleVotes';
import Ranker from './pages/Ranker';
import Performance from './pages/Performance';
import RiskMonitor from './pages/RiskMonitor';
import MLInsights from './pages/MLInsights';
import Controls from './pages/Controls';
import Activity from './pages/Activity';
import Decisions from './pages/Decisions';
import Governor from './pages/Governor';
import Planner from './pages/Planner';
import DecisionTrace from './pages/DecisionTrace';
import Orchestrator from './pages/Orchestrator';
import Feedback from './pages/Feedback';
import PositionHealth from './pages/PositionHealth';
import ShadowOutcomes from './pages/ShadowOutcomes';
import Reconciliation from './pages/Reconciliation';

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<Overview />} />
          <Route path="trades" element={<ActiveTrades />} />
          <Route path="history" element={<TradeHistory />} />
          <Route path="scanner" element={<Scanner />} />
          <Route path="module-votes" element={<ModuleVotes />} />
          <Route path="ranker" element={<Ranker />} />
          <Route path="performance" element={<Performance />} />
          <Route path="risk" element={<RiskMonitor />} />
          <Route path="ml" element={<MLInsights />} />
          <Route path="decisions" element={<Decisions />} />
          <Route path="governor" element={<Governor />} />
          <Route path="planner" element={<Planner />} />
          <Route path="decision-trace" element={<DecisionTrace />} />
          <Route path="orchestrator" element={<Orchestrator />} />
          <Route path="feedback" element={<Feedback />} />
          <Route path="position-health" element={<PositionHealth />} />
          <Route path="controls" element={<Controls />} />
          <Route path="activity" element={<Activity />} />
          <Route path="shadow" element={<ShadowOutcomes />} />
          <Route path="reconciliation" element={<Reconciliation />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}

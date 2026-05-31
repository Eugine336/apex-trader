import React from 'react';
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import Layout from './components/Layout';
import Overview from './pages/Overview';
import ActiveTrades from './pages/ActiveTrades';
import TradeHistory from './pages/TradeHistory';
import Scanner from './pages/Scanner';
import Performance from './pages/Performance';
import RiskMonitor from './pages/RiskMonitor';
import MLInsights from './pages/MLInsights';
import Controls from './pages/Controls';
import Activity from './pages/Activity';

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<Overview />} />
          <Route path="trades" element={<ActiveTrades />} />
          <Route path="history" element={<TradeHistory />} />
          <Route path="scanner" element={<Scanner />} />
          <Route path="performance" element={<Performance />} />
          <Route path="risk" element={<RiskMonitor />} />
          <Route path="ml" element={<MLInsights />} />
          <Route path="controls" element={<Controls />} />
          <Route path="activity" element={<Activity />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}

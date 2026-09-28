import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';

import { AppShell } from './shell/AppShell';
import { AuthProvider } from './shell/auth';
import { ToastProvider } from './components/toast';
import { Today } from './routes/Today';
import { Portfolio } from './routes/Portfolio';
import { StockResearch } from './routes/StockResearch';
import { Report } from './routes/Report';
import { Discovery } from './routes/Discovery';
import { Models } from './routes/Models';
import { Pipeline } from './routes/Pipeline';

export function App() {
  return (
    <AuthProvider>
      <ToastProvider>
        <BrowserRouter>
          <Routes>
            <Route element={<AppShell />}>
              <Route index element={<Navigate to="/today" replace />} />
              <Route path="/today" element={<Today />} />
              <Route path="/portfolio" element={<Portfolio />} />
              <Route path="/stocks" element={<StockResearch />} />
              <Route path="/report" element={<Report />} />
              <Route path="/discovery" element={<Discovery />} />
              <Route path="/models" element={<Models />} />
              <Route path="/pipeline" element={<Pipeline />} />
              <Route path="*" element={<Navigate to="/today" replace />} />
            </Route>
          </Routes>
        </BrowserRouter>
      </ToastProvider>
    </AuthProvider>
  );
}

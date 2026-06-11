import { BrowserRouter, Navigate, Route, Routes } from 'react-router';
import { StatusPage } from '@/pages/status/StatusPage';

export function Router() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/status" element={<StatusPage />} />
        <Route path="*" element={<Navigate to="/status" replace />} />
      </Routes>
    </BrowserRouter>
  );
}

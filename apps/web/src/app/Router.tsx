import { BrowserRouter, Navigate, Route, Routes } from 'react-router';
import type { ReactNode } from 'react';
import { AuthProvider, useAuth } from '@/contexts/AuthContext';
import { StatusPage } from '@/pages/status/StatusPage';
import { LoginPage } from '@/pages/login/LoginPage';
import { PumpsPage } from '@/pages/pumps/PumpsPage';

function RequireAuth({ children }: { children: ReactNode }) {
  const { isAuthenticated } = useAuth();
  if (isAuthenticated === null) return null;
  if (!isAuthenticated) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

function PublicOnly({ children }: { children: ReactNode }) {
  const { isAuthenticated } = useAuth();
  if (isAuthenticated === null) return null;
  if (isAuthenticated) return <Navigate to="/status" replace />;
  return <>{children}</>;
}

export function Router() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          <Route
            path="/login"
            element={
              <PublicOnly>
                <LoginPage />
              </PublicOnly>
            }
          />
          <Route
            path="/status"
            element={
              <RequireAuth>
                <StatusPage />
              </RequireAuth>
            }
          />
          <Route
            path="/pumps"
            element={
              <RequireAuth>
                <PumpsPage />
              </RequireAuth>
            }
          />
          <Route path="*" element={<Navigate to="/status" replace />} />
        </Routes>
      </BrowserRouter>
    </AuthProvider>
  );
}

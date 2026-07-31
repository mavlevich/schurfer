import { BrowserRouter, Navigate, Route, Routes } from 'react-router';
import { lazy, Suspense, type ReactNode } from 'react';
import { AuthProvider, useAuth } from '@/contexts/AuthContext';

const StatusPage = lazy(() =>
  import('@/pages/status/StatusPage').then((module) => ({ default: module.StatusPage })),
);
const LoginPage = lazy(() =>
  import('@/pages/login/LoginPage').then((module) => ({ default: module.LoginPage })),
);
const PumpsPage = lazy(() =>
  import('@/pages/pumps/PumpsPage').then((module) => ({ default: module.PumpsPage })),
);
const TokenPage = lazy(() =>
  import('@/pages/pumps/TokenPage').then((module) => ({ default: module.TokenPage })),
);
const AccountPage = lazy(() =>
  import('@/pages/account/AccountPage').then((module) => ({ default: module.AccountPage })),
);
const TradesPage = lazy(() =>
  import('@/pages/trades/TradesPage').then((module) => ({ default: module.TradesPage })),
);
const DecisionsPage = lazy(() =>
  import('@/pages/decisions/DecisionsPage').then((module) => ({ default: module.DecisionsPage })),
);
const ResearchPage = lazy(() =>
  import('@/pages/research/ResearchPage').then((module) => ({ default: module.ResearchPage })),
);

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
        <Suspense
          fallback={
            <div className="flex min-h-screen items-center justify-center bg-background text-sm text-muted-foreground">
              Loading...
            </div>
          }
        >
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
            <Route
              path="/pumps/:base"
              element={
                <RequireAuth>
                  <TokenPage />
                </RequireAuth>
              }
            />
            <Route
              path="/account"
              element={
                <RequireAuth>
                  <AccountPage />
                </RequireAuth>
              }
            />
            <Route
              path="/trades"
              element={
                <RequireAuth>
                  <TradesPage />
                </RequireAuth>
              }
            />
            <Route
              path="/decisions"
              element={
                <RequireAuth>
                  <DecisionsPage />
                </RequireAuth>
              }
            />
            <Route
              path="/research"
              element={
                <RequireAuth>
                  <ResearchPage />
                </RequireAuth>
              }
            />
            <Route path="*" element={<Navigate to="/status" replace />} />
          </Routes>
        </Suspense>
      </BrowserRouter>
    </AuthProvider>
  );
}

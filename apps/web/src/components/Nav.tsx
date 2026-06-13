import { NavLink } from 'react-router';
import { Activity, TrendingUp, LogOut } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useAuth } from '@/contexts/AuthContext';

export function Nav() {
  const { logout } = useAuth();

  return (
    <nav className="border-b bg-background px-4 md:px-8">
      <div className="mx-auto flex h-12 max-w-6xl items-center justify-between">
        <div className="flex items-center gap-6">
          <span className="font-bold tracking-tight">Schurfer</span>
          <div className="flex items-center gap-1">
            <NavLink
              to="/status"
              className={({ isActive }) =>
                `flex items-center gap-1.5 rounded px-3 py-1.5 text-sm transition-colors ${
                  isActive
                    ? 'bg-accent text-accent-foreground'
                    : 'text-muted-foreground hover:text-foreground'
                }`
              }
            >
              <Activity className="h-3.5 w-3.5" />
              Status
            </NavLink>
            <NavLink
              to="/pumps"
              className={({ isActive }) =>
                `flex items-center gap-1.5 rounded px-3 py-1.5 text-sm transition-colors ${
                  isActive
                    ? 'bg-accent text-accent-foreground'
                    : 'text-muted-foreground hover:text-foreground'
                }`
              }
            >
              <TrendingUp className="h-3.5 w-3.5" />
              Pump Scanner
            </NavLink>
          </div>
        </div>
        <Button variant="ghost" size="icon" onClick={logout} title="Sign out">
          <LogOut className="h-4 w-4" />
        </Button>
      </div>
    </nav>
  );
}

import { useEffect, useState } from 'react';
import { Activity, Database, Radio, Zap } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { StatusDot } from '@/components/shared/StatusDot';
import { useWebSocket } from '@/hooks/useWebSocket';

type ServiceStatus = 'up' | 'down' | 'unknown';

interface ServiceState {
  postgres: ServiceStatus;
  redis: ServiceStatus;
  nats: ServiceStatus;
  collector: ServiceStatus;
  execution: ServiceStatus;
  telegram_bot: ServiceStatus;
}

interface WsStatusMessage {
  type: 'status';
  data: Partial<ServiceState>;
}

const INITIAL_STATE: ServiceState = {
  postgres: 'unknown',
  redis: 'unknown',
  nats: 'unknown',
  collector: 'unknown',
  execution: 'unknown',
  telegram_bot: 'unknown',
};

const WS_URL =
  typeof window !== 'undefined'
    ? `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws/status`
    : 'ws://localhost:8000/ws/status';

export function StatusPage() {
  const [services, setServices] = useState<ServiceState>(INITIAL_STATE);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);

  const { status: wsStatus } = useWebSocket(WS_URL, {
    onMessage: (data) => {
      const msg = data as WsStatusMessage;
      if (msg.type === 'status') {
        setServices((prev) => ({ ...prev, ...msg.data }));
        setLastUpdated(new Date());
      }
    },
  });

  // Poll via REST when WebSocket is not connected
  useEffect(() => {
    if (wsStatus === 'connected') return;

    const poll = async () => {
      try {
        const res = await fetch('/api/health');
        if (res.ok) {
          const data = (await res.json()) as Partial<ServiceState>;
          setServices((prev) => ({ ...prev, ...data }));
          setLastUpdated(new Date());
        }
      } catch {
        // api-gateway not reachable
      }
    };

    poll();
    const interval = setInterval(poll, 10_000);
    return () => clearInterval(interval);
  }, [wsStatus]);

  const infra = [
    { key: 'postgres' as const, label: 'PostgreSQL', icon: Database },
    { key: 'redis' as const, label: 'Redis', icon: Zap },
    { key: 'nats' as const, label: 'NATS', icon: Radio },
  ];

  const apps = [
    { key: 'collector' as const, label: 'Collector' },
    { key: 'execution' as const, label: 'Execution' },
    { key: 'telegram_bot' as const, label: 'Telegram Bot' },
  ];

  const allUp = Object.values(services).every((s) => s === 'up');
  const anyDown = Object.values(services).some((s) => s === 'down');

  return (
    <div className="min-h-screen bg-background p-4 md:p-8">
      <div className="mx-auto max-w-2xl space-y-6">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold tracking-tight">Schurfer</h1>
            <p className="text-sm text-muted-foreground">System Status</p>
          </div>
          <div className="flex items-center gap-2">
            <StatusDot status={wsStatus === 'connected' ? 'up' : 'unknown'} />
            <span className="text-xs text-muted-foreground">
              {wsStatus === 'connected' ? 'Live' : 'Polling'}
            </span>
          </div>
        </div>

        {/* Overall status */}
        <Card>
          <CardContent className="flex items-center gap-3 py-4">
            <Activity className="h-5 w-5 text-muted-foreground" />
            <div className="flex-1">
              <p className="text-sm font-medium">
                {anyDown
                  ? 'Some services are down'
                  : allUp
                    ? 'All systems operational'
                    : 'Checking services...'}
              </p>
              {lastUpdated && (
                <p className="text-xs text-muted-foreground">
                  Updated {lastUpdated.toLocaleTimeString()}
                </p>
              )}
            </div>
            <Badge variant={anyDown ? 'destructive' : allUp ? 'success' : 'secondary'}>
              {anyDown ? 'Degraded' : allUp ? 'Operational' : 'Unknown'}
            </Badge>
          </CardContent>
        </Card>

        {/* Infrastructure */}
        <Card>
          <CardHeader>
            <CardTitle className="text-sm font-medium text-muted-foreground uppercase tracking-wider">
              Infrastructure
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 pt-0">
            {infra.map(({ key, label, icon: Icon }) => (
              <div key={key} className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <Icon className="h-4 w-4 text-muted-foreground" />
                  <span className="text-sm">{label}</span>
                </div>
                <div className="flex items-center gap-2">
                  <StatusDot status={services[key]} />
                  <span className="text-xs text-muted-foreground capitalize">{services[key]}</span>
                </div>
              </div>
            ))}
          </CardContent>
        </Card>

        {/* Services */}
        <Card>
          <CardHeader>
            <CardTitle className="text-sm font-medium text-muted-foreground uppercase tracking-wider">
              Services
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 pt-0">
            {apps.map(({ key, label }) => (
              <div key={key} className="flex items-center justify-between">
                <span className="text-sm">{label}</span>
                <div className="flex items-center gap-2">
                  <StatusDot status={services[key]} />
                  <span className="text-xs text-muted-foreground capitalize">{services[key]}</span>
                </div>
              </div>
            ))}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

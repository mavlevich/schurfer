import { useEffect, useState } from 'react';
import { Activity, Database, Radio, ScanSearch, Zap } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Nav } from '@/components/Nav';
import { StatusDot } from '@/components/shared/StatusDot';
import { useWebSocket } from '@/hooks/useWebSocket';

type ServiceStatus = 'up' | 'down' | 'unknown';

interface ScannerState {
  ts: number;
  count: number;
  scanned?: string[];
  errors?: Record<string, string>;
}

interface SignalReadinessState {
  updated_at_ms: number;
  pump_count: number;
  evaluated: number;
  ready: number;
  deferred: number;
  reasons: Record<string, number>;
}

interface ServiceState {
  postgres: ServiceStatus;
  redis: ServiceStatus;
  nats: ServiceStatus;
  collector: ServiceStatus;
  execution: ServiceStatus;
  telegram_bot: ServiceStatus;
  signal_readiness: SignalReadinessState | null;
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
  signal_readiness: null,
};

const WS_URL =
  typeof window !== 'undefined'
    ? `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws/status`
    : 'ws://localhost:8000/ws/status';

function timeAgo(tsMs: number): string {
  const secs = Math.floor((Date.now() - tsMs) / 1000);
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  return `${Math.floor(secs / 3600)}h ago`;
}

export function StatusPage() {
  const [services, setServices] = useState<ServiceState>(INITIAL_STATE);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [scanner, setScanner] = useState<ScannerState | null>(null);

  const { status: wsStatus } = useWebSocket(WS_URL, {
    onMessage: (data) => {
      const msg = data as WsStatusMessage;
      if (msg.type === 'status') {
        setServices((prev) => ({ ...prev, ...msg.data }));
        setLastUpdated(new Date());
      }
    },
  });

  // When WS drops, clear stale statuses so we don't show ghost "up" state.
  useEffect(() => {
    if (wsStatus === 'disconnected' || wsStatus === 'error') {
      setServices(INITIAL_STATE);
    }
  }, [wsStatus]);

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
        } else {
          setServices(INITIAL_STATE);
        }
      } catch {
        setServices(INITIAL_STATE);
      }
    };

    poll();
    const interval = setInterval(poll, 10_000);
    return () => clearInterval(interval);
  }, [wsStatus]);

  // Poll pump scanner stats
  useEffect(() => {
    const pollScanner = async () => {
      try {
        const res = await fetch('/api/pumps');
        if (res.ok) setScanner((await res.json()) as ScannerState);
      } catch {
        // ignore
      }
    };
    pollScanner();
    const interval = setInterval(pollScanner, 30_000);
    return () => clearInterval(interval);
  }, []);

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

  const serviceStatuses = [...infra, ...apps].map(({ key }) => services[key]);
  const allUp = serviceStatuses.every((status) => status === 'up');
  const anyDown = serviceStatuses.some((status) => status === 'down');
  const signalReadiness = services.signal_readiness;
  const readinessVariant =
    signalReadiness && signalReadiness.evaluated > 0 && signalReadiness.deferred === 0
      ? 'success'
      : signalReadiness && signalReadiness.ready === 0 && signalReadiness.deferred > 0
        ? 'destructive'
        : 'secondary';

  return (
    <div className="min-h-screen bg-background">
      <Nav />
      <div className="mx-auto max-w-2xl space-y-6 p-4 md:p-8">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-xl font-bold tracking-tight">System Status</h1>
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

        {/* Pump scanner stats */}
        <Card>
          <CardHeader>
            <CardTitle className="text-sm font-medium text-muted-foreground uppercase tracking-wider">
              Pump Scanner
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 pt-0">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <ScanSearch className="h-4 w-4 text-muted-foreground" />
                <span className="text-sm">Last scan</span>
              </div>
              <span className="text-xs text-muted-foreground">
                {scanner?.ts ? timeAgo(scanner.ts) : '—'}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <Activity className="h-4 w-4 text-muted-foreground" />
                <span className="text-sm">Active pumps</span>
              </div>
              <span className="text-xs font-mono text-muted-foreground">
                {scanner ? `${scanner.count} tokens` : '—'}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <Radio className="h-4 w-4 text-muted-foreground" />
                <span className="text-sm">Exchanges</span>
              </div>
              <span className="text-xs font-mono text-muted-foreground">
                {scanner?.scanned
                  ? `${scanner.scanned.length} ok${Object.keys(scanner.errors ?? {}).length ? ` · ${Object.keys(scanner.errors ?? {}).length} failed` : ''}`
                  : '—'}
              </span>
            </div>
          </CardContent>
        </Card>

        {/* Execution input readiness */}
        <Card>
          <CardHeader className="flex-row items-center justify-between space-y-0">
            <CardTitle className="text-sm font-medium text-muted-foreground uppercase tracking-wider">
              Signal Readiness
            </CardTitle>
            <Badge variant={readinessVariant}>
              {!signalReadiness
                ? 'No telemetry'
                : signalReadiness.evaluated === 0
                  ? 'Idle'
                  : signalReadiness.deferred === 0
                    ? 'Ready'
                    : `${signalReadiness.deferred} deferred`}
            </Badge>
          </CardHeader>
          <CardContent className="space-y-3 pt-0">
            <div className="flex items-center justify-between">
              <span className="text-sm">Latest trader tick</span>
              <span className="text-xs text-muted-foreground">
                {signalReadiness ? timeAgo(signalReadiness.updated_at_ms) : '—'}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-sm">Pumps / evaluated</span>
              <span className="text-xs font-mono text-muted-foreground">
                {signalReadiness
                  ? `${signalReadiness.pump_count} / ${signalReadiness.evaluated}`
                  : '—'}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-sm">Ready / deferred</span>
              <span className="text-xs font-mono text-muted-foreground">
                {signalReadiness ? `${signalReadiness.ready} / ${signalReadiness.deferred}` : '—'}
              </span>
            </div>
            {signalReadiness &&
              Object.entries(signalReadiness.reasons).map(([reason, count]) => (
                <div key={reason} className="flex items-center justify-between">
                  <span className="text-xs text-muted-foreground">{reason}</span>
                  <span className="text-xs font-mono text-muted-foreground">{count}</span>
                </div>
              ))}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

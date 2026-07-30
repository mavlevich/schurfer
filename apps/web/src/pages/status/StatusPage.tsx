import { useEffect, useState } from 'react';
import {
  Activity,
  Cpu,
  Database,
  Gauge,
  HardDrive,
  MemoryStick,
  Radio,
  ScanSearch,
  Zap,
} from 'lucide-react';
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

interface SystemLoadState {
  captured_at_ms: number;
  cpu_count: number;
  load_1m: number;
  load_5m: number;
  load_15m: number;
  memory_used_bytes: number;
  memory_total_bytes: number;
  memory_used_pct: number;
  disk_used_bytes: number;
  disk_total_bytes: number;
  disk_used_pct: number;
  system_uptime_seconds: number;
}

interface MarketPipelineState {
  updated_at_ms: number;
  observed_symbols: number;
  hot_symbols: number;
  event_rate_per_sec: number;
  last_lag_ms: number;
  max_lag_ms: number;
  nats_dropped_total: number;
  pending_dropped_total: number;
  persist_errors_total: number;
  bars_persisted_total: number;
  pump_feed_status: string;
}

interface ServiceState {
  postgres: ServiceStatus;
  redis: ServiceStatus;
  nats: ServiceStatus;
  collector: ServiceStatus;
  execution: ServiceStatus;
  telegram_bot: ServiceStatus;
  signal_readiness: SignalReadinessState | null;
  system_load: SystemLoadState | null;
  market_pipeline: MarketPipelineState | null;
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
  system_load: null,
  market_pipeline: null,
};

const WS_URL =
  typeof window !== 'undefined'
    ? `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws/status`
    : 'ws://localhost:8000/ws/status';

function timeAgo(tsMs: number): string {
  const secs = Math.max(0, Math.floor((Date.now() - tsMs) / 1000));
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  return `${Math.floor(secs / 3600)}h ago`;
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value < 0) return 'n/a';
  const gib = value / 1024 ** 3;
  if (gib >= 1) return `${gib.toFixed(gib >= 10 ? 1 : 2)} GiB`;
  return `${(value / 1024 ** 2).toFixed(0)} MiB`;
}

function formatUptime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return 'n/a';
  const days = Math.floor(seconds / 86_400);
  const hours = Math.floor((seconds % 86_400) / 3_600);
  return days > 0 ? `${days}d ${hours}h` : `${hours}h`;
}

function LoadBar({
  label,
  value,
  detail,
  icon: Icon,
}: {
  label: string;
  value: number;
  detail: string;
  icon: typeof Cpu;
}) {
  const bounded = Math.max(0, Math.min(value, 100));
  const color = value >= 80 ? 'bg-red-500' : value >= 65 ? 'bg-amber-500' : 'bg-emerald-500';
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <Icon className="h-4 w-4 text-muted-foreground" />
          <span className="text-sm">{label}</span>
        </div>
        <span className="text-xs font-mono text-muted-foreground">
          {value.toFixed(1)}% · {detail}
        </span>
      </div>
      <div className="ml-7 h-1.5 overflow-hidden rounded-full bg-muted">
        <div className={`h-full rounded-full ${color}`} style={{ width: `${bounded}%` }} />
      </div>
    </div>
  );
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
  const systemLoad = services.system_load;
  const marketPipeline = services.market_pipeline;
  const normalizedCPULoad =
    systemLoad && systemLoad.cpu_count > 0 ? (systemLoad.load_1m / systemLoad.cpu_count) * 100 : 0;
  const pipelineAgeMS = marketPipeline ? Date.now() - marketPipeline.updated_at_ms : null;
  const pipelineFresh = pipelineAgeMS !== null && pipelineAgeMS >= 0 && pipelineAgeMS < 60_000;
  const pipelineDrops = marketPipeline
    ? marketPipeline.nats_dropped_total + marketPipeline.pending_dropped_total
    : 0;
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

        {/* Host resource load */}
        <Card>
          <CardHeader className="flex-row items-center justify-between space-y-0">
            <CardTitle className="text-sm font-medium text-muted-foreground uppercase tracking-wider">
              Server Load
            </CardTitle>
            <Badge
              variant={
                systemLoad && Math.max(systemLoad.disk_used_pct, systemLoad.memory_used_pct) >= 80
                  ? 'destructive'
                  : systemLoad
                    ? 'success'
                    : 'secondary'
              }
            >
              {systemLoad ? formatUptime(systemLoad.system_uptime_seconds) : 'No telemetry'}
            </Badge>
          </CardHeader>
          <CardContent className="space-y-4 pt-0">
            {systemLoad ? (
              <>
                <LoadBar
                  label="CPU load"
                  value={normalizedCPULoad}
                  detail={`${systemLoad.load_1m.toFixed(2)} / ${systemLoad.cpu_count} CPUs`}
                  icon={Cpu}
                />
                <LoadBar
                  label="Memory"
                  value={systemLoad.memory_used_pct}
                  detail={`${formatBytes(systemLoad.memory_used_bytes)} / ${formatBytes(systemLoad.memory_total_bytes)}`}
                  icon={MemoryStick}
                />
                <LoadBar
                  label="Disk"
                  value={systemLoad.disk_used_pct}
                  detail={`${formatBytes(systemLoad.disk_used_bytes)} / ${formatBytes(systemLoad.disk_total_bytes)}`}
                  icon={HardDrive}
                />
              </>
            ) : (
              <p className="text-xs text-muted-foreground">
                Host metrics are unavailable in this runtime.
              </p>
            )}
          </CardContent>
        </Card>

        {/* Market stream load */}
        <Card>
          <CardHeader className="flex-row items-center justify-between space-y-0">
            <CardTitle className="text-sm font-medium text-muted-foreground uppercase tracking-wider">
              Market Pipeline
            </CardTitle>
            <Badge
              variant={!marketPipeline ? 'secondary' : pipelineFresh ? 'success' : 'destructive'}
            >
              {!marketPipeline ? 'No telemetry' : pipelineFresh ? 'Live' : 'Stale'}
            </Badge>
          </CardHeader>
          <CardContent className="space-y-3 pt-0">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <Gauge className="h-4 w-4 text-muted-foreground" />
                <span className="text-sm">Ticker throughput</span>
              </div>
              <span className="text-xs font-mono text-muted-foreground">
                {marketPipeline
                  ? `${marketPipeline.event_rate_per_sec.toFixed(0)} events/s`
                  : 'n/a'}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <Radio className="h-4 w-4 text-muted-foreground" />
                <span className="text-sm">Observed / hot symbols</span>
              </div>
              <span className="text-xs font-mono text-muted-foreground">
                {marketPipeline
                  ? `${marketPipeline.observed_symbols} / ${marketPipeline.hot_symbols}`
                  : 'n/a'}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-sm">Latest / maximum lag</span>
              <span className="text-xs font-mono text-muted-foreground">
                {marketPipeline
                  ? `${marketPipeline.last_lag_ms} / ${marketPipeline.max_lag_ms} ms`
                  : 'n/a'}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-sm">Drops / persistence errors</span>
              <span className="text-xs font-mono text-muted-foreground">
                {marketPipeline
                  ? `${pipelineDrops} / ${marketPipeline.persist_errors_total}`
                  : 'n/a'}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-sm">Last telemetry</span>
              <span className="text-xs text-muted-foreground">
                {marketPipeline ? timeAgo(marketPipeline.updated_at_ms) : 'n/a'}
              </span>
            </div>
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
                {scanner?.ts ? timeAgo(scanner.ts) : 'n/a'}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <Activity className="h-4 w-4 text-muted-foreground" />
                <span className="text-sm">Active pumps</span>
              </div>
              <span className="text-xs font-mono text-muted-foreground">
                {scanner ? `${scanner.count} tokens` : 'n/a'}
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
                  : 'n/a'}
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
                {signalReadiness ? timeAgo(signalReadiness.updated_at_ms) : 'n/a'}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-sm">Pumps / evaluated</span>
              <span className="text-xs font-mono text-muted-foreground">
                {signalReadiness
                  ? `${signalReadiness.pump_count} / ${signalReadiness.evaluated}`
                  : 'n/a'}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-sm">Ready / deferred</span>
              <span className="text-xs font-mono text-muted-foreground">
                {signalReadiness ? `${signalReadiness.ready} / ${signalReadiness.deferred}` : 'n/a'}
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

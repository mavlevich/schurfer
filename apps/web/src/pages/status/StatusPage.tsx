import { useEffect, useState } from 'react';
import {
  Activity,
  AlertTriangle,
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
import { PageShell } from '@/components/shared/PageShell';
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
  cpu_utilization_pct: number | null;
  load_pressure_pct: number;
  load_1m: number;
  load_5m: number;
  load_15m: number;
  memory_used_bytes: number;
  memory_total_bytes: number;
  memory_used_pct: number;
  mem_available_bytes: number;
  swap_used_bytes: number;
  swap_total_bytes: number;
  swap_used_pct: number;
  swap_in_bytes_per_sec: number | null;
  swap_out_bytes_per_sec: number | null;
  disk_used_bytes: number;
  disk_total_bytes: number;
  disk_used_pct: number;
  system_uptime_seconds: number;
}

// DiskUsageState breaks down host disk usage into what a deploy-time cleanup
// decision actually needs: reclaimable build artifacts (Docker images,
// build cache) versus real data (Postgres, backups) that must never be
// pruned. See health.DiskUsage's own doc comment for why this comes from a
// host-side snapshot file rather than the api-gateway container itself.
interface DiskUsageState {
  captured_at_ms: number;
  images_bytes: number;
  images_reclaimable_bytes: number;
  containers_bytes: number;
  volumes_bytes: number;
  build_cache_bytes: number;
  build_cache_reclaimable_bytes: number;
  postgres_data_bytes: number;
  backups_bytes: number;
}

interface ContainerMetricState {
  name: string;
  cpu_percent: number;
  memory_used_bytes: number;
  memory_limit_bytes: number;
  memory_used_pct: number;
  pids: number;
  status: string;
  health: string;
  restart_count: number;
  started_at: string;
  oom_killed: boolean;
}

interface ContainerRuntimeState {
  captured_at_ms: number;
  total_cpu_percent: number;
  total_memory_used_bytes: number;
  containers: ContainerMetricState[];
}

interface MarketPipelineState {
  updated_at_ms: number;
  observed_symbols: number;
  hot_symbols: number;
  event_rate_per_sec: number;
  last_lag_ms: number;
  max_lag_ms: number;
  window_max_lag_ms: number;
  nats_dropped_total: number;
  pending_dropped_total: number;
  persist_errors_total: number;
  bars_persisted_total: number;
  pump_feed_status: string;
}

interface OrderflowPilotState {
  updated_at_ms: number;
  started_at_ms: number;
  status: string;
  observed_symbols: number;
  event_rate_per_sec: number;
  active_captures: number;
  activation_total: number;
  records_persisted_total: number;
  storage_bytes: number;
  storage_bytes_per_day: number;
  last_lag_ms: number;
  window_max_lag_ms: number;
  queue_dropped_total: number;
  pending_dropped_total: number;
  persist_errors_total: number;
  storage_limited_total: number;
  left_censored_total: number;
  capacity_rejected_total: number;
}

interface FillIncidentSummary {
  id: number;
  exchange: string;
  base: string;
  operation: string;
  order_id: string;
  status: string;
  attempt_count: number;
  last_error: string | null;
  created_at: string;
}

interface FillIncidentsState {
  pnl_ready: boolean;
  open: FillIncidentSummary[];
}

interface ResourceSample {
  captured_at_ms: number;
  cpu_pct: number | null;
  memory_pct: number;
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
  disk_usage: DiskUsageState | null;
  container_runtime: ContainerRuntimeState | null;
  market_pipeline: MarketPipelineState | null;
  orderflow_pilot: OrderflowPilotState | null;
  fill_incidents: FillIncidentsState | null;
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
  disk_usage: null,
  container_runtime: null,
  market_pipeline: null,
  orderflow_pilot: null,
  fill_incidents: null,
};

const WS_URL =
  typeof window !== 'undefined'
    ? `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws/status`
    : 'ws://localhost:8000/ws/status';

// Below this much real MemAvailable, a new process risks swapping before it
// even finishes starting up. Chosen to sit below the combined RAM+swap
// budget the ad-hoc analytics reports already require (see Makefile's
// PROD_REPORT_MIN_HEADROOM_MB), so this warning trips before those reports
// would refuse to start, not after.
const LOW_MEM_AVAILABLE_THRESHOLD_BYTES = 768 * 1024 * 1024;

// Above this much reclaimable Docker build cache, flag it: a 2026-08-16
// incident found 15.6 GiB of stale build cache (a third of the disk used at
// the time) that `make prod-deploy`'s own `docker image prune -f` never
// touches (it only prunes dangling images, not the builder's own layer
// cache) -- unbounded growth across every deploy, not a one-time thing.
const HIGH_RECLAIMABLE_BUILD_CACHE_THRESHOLD_BYTES = 5 * 1024 ** 3;

// Containers deliberately left stopped as part of a past decision, not a
// crash. An "exited" container outside this set is treated as unexpected.
const RETIRED_CONTAINER_NAMES = new Set(['schurfer-orderflow-pilot']);

type ContainerSeverity = 'bad' | 'retired' | 'stale' | 'ok';

function containerSeverity(container: ContainerMetricState, fresh: boolean): ContainerSeverity {
  if (
    container.oom_killed ||
    container.status === 'restarting' ||
    container.health === 'unhealthy'
  ) {
    return 'bad';
  }
  if (container.status === 'exited') {
    return RETIRED_CONTAINER_NAMES.has(container.name) ? 'retired' : 'bad';
  }
  if (!fresh) return 'stale';
  return 'ok';
}

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

// containerDisplayName is a display-only relabel, not a rename: the
// underlying container/service name (docker-compose, Makefile targets,
// the canary checkpoint script, Redis health keys) stays "momentum-
// capture" -- Bybit was the only venue when it was named, so it carries
// no exchange suffix, unlike every venue added since ("momentum-capture-
// binance", "momentum-watch-binance"). Renaming the actual container
// would mean rebuilding and restarting the live Bybit canary process for
// a purely cosmetic fix (see ROADMAP.md's own tech-debt note on this);
// this map only clarifies what the operator is looking at.
const CONTAINER_DISPLAY_NAMES: Record<string, string> = {
  'momentum-capture': 'momentum-capture (bybit)',
};

function containerDisplayName(strippedName: string): string {
  return CONTAINER_DISPLAY_NAMES[strippedName] ?? strippedName;
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

interface DropTrackingState {
  previousTotal: number | null;
  previousUpdatedAtMs: number | null;
  recentDelta: number;
  recentWindowMs: number | null;
  lastDropAtMs: number | null;
}

const INITIAL_DROP_TRACKING: DropTrackingState = {
  previousTotal: null,
  previousUpdatedAtMs: null,
  recentDelta: 0,
  recentWindowMs: null,
  lastDropAtMs: null,
};

export function StatusPage() {
  const [services, setServices] = useState<ServiceState>(INITIAL_STATE);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [scanner, setScanner] = useState<ScannerState | null>(null);
  const [resourceHistory, setResourceHistory] = useState<ResourceSample[]>([]);
  const [dropTracking, setDropTracking] = useState<DropTrackingState>(INITIAL_DROP_TRACKING);

  const recordResourceSample = (load: SystemLoadState | null | undefined) => {
    if (!load) return;
    setResourceHistory((previous) => {
      const cutoff = Date.now() - 60 * 60 * 1000;
      const withoutDuplicate = previous.filter(
        (sample) => sample.captured_at_ms !== load.captured_at_ms,
      );
      return [
        ...withoutDuplicate,
        {
          captured_at_ms: load.captured_at_ms,
          cpu_pct: load.cpu_utilization_pct,
          memory_pct: load.memory_used_pct,
        },
      ].filter((sample) => sample.captured_at_ms >= cutoff);
    });
  };

  // Drops are a lifetime cumulative counter: comparing consecutive samples is
  // the only way to tell "already happened once, long ago" apart from
  // "happening right now". The first sample of a fresh page load never
  // counts as a fresh drop, however large the lifetime total already is.
  const recordDropSample = (pipeline: MarketPipelineState | null | undefined) => {
    if (!pipeline) return;
    const total = pipeline.nats_dropped_total + pipeline.pending_dropped_total;
    setDropTracking((previous) => {
      if (previous.previousUpdatedAtMs === pipeline.updated_at_ms) return previous;
      const delta =
        previous.previousTotal === null ? 0 : Math.max(0, total - previous.previousTotal);
      const windowMs =
        previous.previousUpdatedAtMs === null
          ? null
          : pipeline.updated_at_ms - previous.previousUpdatedAtMs;
      return {
        previousTotal: total,
        previousUpdatedAtMs: pipeline.updated_at_ms,
        recentDelta: delta,
        recentWindowMs: windowMs,
        lastDropAtMs: delta > 0 ? pipeline.updated_at_ms : previous.lastDropAtMs,
      };
    });
  };

  const { status: wsStatus } = useWebSocket(WS_URL, {
    onMessage: (data) => {
      const msg = data as WsStatusMessage;
      if (msg.type === 'status') {
        setServices((prev) => ({ ...prev, ...msg.data }));
        recordResourceSample(msg.data.system_load);
        recordDropSample(msg.data.market_pipeline);
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
          recordResourceSample(data.system_load);
          recordDropSample(data.market_pipeline);
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

  const signalReadiness = services.signal_readiness;
  const systemLoad = services.system_load;
  const diskUsage = services.disk_usage;
  const containerRuntime = services.container_runtime;
  const marketPipeline = services.market_pipeline;
  const fillIncidents = services.fill_incidents;
  const hasOpenFillIncidents = !!fillIncidents && fillIncidents.open.length > 0;
  const cpuHistory = resourceHistory
    .map((sample) => sample.cpu_pct)
    .filter((value): value is number => value !== null);
  const cpuPeak = cpuHistory.length > 0 ? Math.max(...cpuHistory) : null;
  const memoryPeak =
    resourceHistory.length > 0
      ? Math.max(...resourceHistory.map((sample) => sample.memory_pct))
      : null;
  const containerAgeMS = containerRuntime ? Date.now() - containerRuntime.captured_at_ms : null;
  const containerFresh = containerAgeMS !== null && containerAgeMS >= 0 && containerAgeMS < 30_000;
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

  // Overall status is three-tiered, not just up/down: a critical dependency
  // being down is Degraded, but low memory headroom, active swap churn, or
  // drops happening right now are Operational-with-warnings, not Degraded,
  // since the service is still working, just worth a human's attention.
  const criticalServiceKeys = ['postgres', 'redis', 'nats', 'collector', 'execution'] as const;
  const criticalDown = criticalServiceKeys.some((key) => services[key] === 'down');
  const containerSeverities = (containerRuntime?.containers ?? []).map((container) =>
    containerSeverity(container, containerFresh),
  );
  const anyContainerBad = containerSeverities.includes('bad');
  const degraded = criticalDown || anyContainerBad;

  const lowMemoryHeadroom = systemLoad
    ? systemLoad.mem_available_bytes < LOW_MEM_AVAILABLE_THRESHOLD_BYTES
    : false;
  const activeSwapChurn = systemLoad
    ? (systemLoad.swap_in_bytes_per_sec ?? 0) > 0 || (systemLoad.swap_out_bytes_per_sec ?? 0) > 0
    : false;
  const dropsIncreasingNow = dropTracking.recentDelta > 0;
  const telegramDown = services.telegram_bot === 'down';
  const containerTelemetryStale = !!containerRuntime && !containerFresh && !anyContainerBad;
  const hasWarnings =
    !degraded &&
    (lowMemoryHeadroom ||
      activeSwapChurn ||
      dropsIncreasingNow ||
      telegramDown ||
      containerTelemetryStale);
  const overallStatus: 'degraded' | 'warning' | 'operational' = degraded
    ? 'degraded'
    : hasWarnings
      ? 'warning'
      : 'operational';
  const overallLabel =
    overallStatus === 'degraded'
      ? 'Degraded'
      : overallStatus === 'warning'
        ? 'Operational with warnings'
        : 'Operational';
  const overallMessage =
    overallStatus === 'degraded'
      ? 'Some services are down'
      : overallStatus === 'warning'
        ? 'Operational, with items worth a look'
        : 'All systems operational';
  const overallVariant =
    overallStatus === 'degraded'
      ? 'destructive'
      : overallStatus === 'warning'
        ? 'warning'
        : 'success';

  return (
    <PageShell width="content">
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
            <p className="text-sm font-medium">{overallMessage}</p>
            {lastUpdated && (
              <p className="text-xs text-muted-foreground">
                Updated {lastUpdated.toLocaleTimeString()}
              </p>
            )}
          </div>
          <Badge variant={overallVariant}>{overallLabel}</Badge>
        </CardContent>
      </Card>

      {/* Fill resolution / PnL readiness */}
      <Card>
        <CardHeader className="flex-row items-center justify-between space-y-0">
          <CardTitle className="text-sm font-medium text-muted-foreground uppercase tracking-wider">
            Fill Resolution
          </CardTitle>
          <Badge
            variant={
              !fillIncidents
                ? 'secondary'
                : hasOpenFillIncidents
                  ? 'destructive'
                  : fillIncidents.pnl_ready
                    ? 'success'
                    : 'warning'
            }
          >
            {!fillIncidents
              ? 'No telemetry'
              : hasOpenFillIncidents
                ? `${fillIncidents.open.length} unresolved`
                : fillIncidents.pnl_ready
                  ? 'PnL confirmed'
                  : 'PnL not ready'}
          </Badge>
        </CardHeader>
        <CardContent className="space-y-3 pt-0">
          {!fillIncidents ? (
            <p className="text-xs text-muted-foreground">Fill-incident telemetry is unavailable.</p>
          ) : (
            <>
              <div className="flex items-center gap-3">
                <AlertTriangle
                  className={`h-4 w-4 ${fillIncidents.pnl_ready ? 'text-muted-foreground' : 'text-amber-500'}`}
                />
                <p className="text-xs text-muted-foreground">
                  {fillIncidents.pnl_ready
                    ? 'PnL readiness lease is valid — every recent fill resolved to a real exchange price.'
                    : 'PnL readiness lease is revoked. A fill price could not be confirmed — treat displayed PnL as provisional until it resolves.'}
                </p>
              </div>
              {hasOpenFillIncidents && (
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[560px] text-left text-xs">
                    <thead className="text-muted-foreground">
                      <tr className="border-b">
                        <th className="py-2 font-medium">Exchange / base</th>
                        <th className="py-2 font-medium">Operation</th>
                        <th className="py-2 font-medium">Order</th>
                        <th className="py-2 font-medium">Status</th>
                        <th className="py-2 text-right font-medium">Attempts</th>
                        <th className="py-2 font-medium">Age</th>
                        <th className="py-2 font-medium">Last error</th>
                      </tr>
                    </thead>
                    <tbody>
                      {fillIncidents.open.map((incident) => (
                        <tr key={incident.id} className="border-b last:border-0">
                          <td className="py-2 font-mono">
                            {incident.exchange}/{incident.base}
                          </td>
                          <td className="py-2">{incident.operation}</td>
                          <td className="py-2 font-mono">{incident.order_id}</td>
                          <td className="py-2">
                            <span
                              className={
                                incident.status === 'manual_required'
                                  ? 'text-red-500'
                                  : 'text-amber-500'
                              }
                            >
                              {incident.status}
                            </span>
                          </td>
                          <td className="py-2 text-right font-mono">{incident.attempt_count}</td>
                          <td className="py-2 text-muted-foreground">
                            {timeAgo(new Date(incident.created_at).getTime())}
                          </td>
                          <td className="py-2 text-muted-foreground">
                            {incident.last_error ?? '—'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
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
              lowMemoryHeadroom || activeSwapChurn
                ? 'warning'
                : systemLoad && Math.max(systemLoad.disk_used_pct, systemLoad.memory_used_pct) >= 80
                  ? 'destructive'
                  : systemLoad
                    ? 'success'
                    : 'secondary'
            }
          >
            {!systemLoad
              ? 'No telemetry'
              : lowMemoryHeadroom
                ? 'Low memory headroom'
                : activeSwapChurn
                  ? 'Active swap churn'
                  : formatUptime(systemLoad.system_uptime_seconds)}
          </Badge>
        </CardHeader>
        <CardContent className="space-y-4 pt-0">
          {systemLoad ? (
            <>
              {systemLoad.cpu_utilization_pct === null ? (
                <p className="text-xs text-muted-foreground">
                  Measuring CPU utilization. The next sample will contain an interval value.
                </p>
              ) : (
                <LoadBar
                  label="CPU utilization"
                  value={systemLoad.cpu_utilization_pct}
                  detail={`${systemLoad.cpu_count} CPUs${cpuPeak === null ? '' : ` · 60m peak ${cpuPeak.toFixed(1)}%`}`}
                  icon={Cpu}
                />
              )}
              <LoadBar
                label="CPU pressure"
                value={systemLoad.load_pressure_pct}
                detail={`${systemLoad.load_1m.toFixed(2)} load / ${systemLoad.cpu_count} CPUs`}
                icon={Gauge}
              />
              <p className="ml-7 text-xs text-muted-foreground">
                Pressure can exceed 100%. It measures runnable work, not CPU time.
              </p>
              <LoadBar
                label="Memory"
                value={systemLoad.memory_used_pct}
                detail={`${formatBytes(systemLoad.memory_used_bytes)} / ${formatBytes(systemLoad.memory_total_bytes)}${memoryPeak === null ? '' : ` · 60m peak ${memoryPeak.toFixed(1)}%`}`}
                icon={MemoryStick}
              />
              <div className="ml-7 flex items-center justify-between">
                <span
                  className={`text-xs ${lowMemoryHeadroom ? 'text-amber-500' : 'text-muted-foreground'}`}
                >
                  Real available (MemAvailable)
                </span>
                <span
                  className={`text-xs font-mono ${lowMemoryHeadroom ? 'text-amber-500' : 'text-muted-foreground'}`}
                >
                  {formatBytes(systemLoad.mem_available_bytes)}
                </span>
              </div>
              {systemLoad.swap_total_bytes > 0 && (
                <>
                  <LoadBar
                    label="Swap"
                    value={systemLoad.swap_used_pct}
                    detail={`${formatBytes(systemLoad.swap_used_bytes)} / ${formatBytes(systemLoad.swap_total_bytes)}`}
                    icon={MemoryStick}
                  />
                  <div className="ml-7 flex items-center justify-between">
                    <span
                      className={`text-xs ${activeSwapChurn ? 'text-amber-500' : 'text-muted-foreground'}`}
                    >
                      Swap activity (in / out)
                    </span>
                    <span
                      className={`text-xs font-mono ${activeSwapChurn ? 'text-amber-500' : 'text-muted-foreground'}`}
                    >
                      {systemLoad.swap_in_bytes_per_sec === null
                        ? 'measuring...'
                        : `${formatBytes(systemLoad.swap_in_bytes_per_sec)}/s / ${formatBytes(
                            systemLoad.swap_out_bytes_per_sec ?? 0,
                          )}/s`}
                    </span>
                  </div>
                  {!activeSwapChurn && systemLoad.swap_used_bytes > 0 && (
                    <p className="ml-7 text-xs text-muted-foreground">
                      Swap is in use but not actively paging right now: usage alone is not a current
                      problem.
                    </p>
                  )}
                </>
              )}
              <LoadBar
                label="Disk"
                value={systemLoad.disk_used_pct}
                detail={`${formatBytes(systemLoad.disk_used_bytes)} / ${formatBytes(systemLoad.disk_total_bytes)}`}
                icon={HardDrive}
              />
              {diskUsage && (
                <div className="ml-7 space-y-1.5">
                  <div className="flex items-center justify-between">
                    <span
                      className={`text-xs ${
                        diskUsage.build_cache_reclaimable_bytes >=
                        HIGH_RECLAIMABLE_BUILD_CACHE_THRESHOLD_BYTES
                          ? 'text-amber-500'
                          : 'text-muted-foreground'
                      }`}
                    >
                      Docker build cache (reclaimable)
                    </span>
                    <span
                      className={`text-xs font-mono ${
                        diskUsage.build_cache_reclaimable_bytes >=
                        HIGH_RECLAIMABLE_BUILD_CACHE_THRESHOLD_BYTES
                          ? 'text-amber-500'
                          : 'text-muted-foreground'
                      }`}
                    >
                      {formatBytes(diskUsage.build_cache_bytes)}
                    </span>
                  </div>
                  {diskUsage.build_cache_reclaimable_bytes >=
                    HIGH_RECLAIMABLE_BUILD_CACHE_THRESHOLD_BYTES && (
                    <p className="text-xs text-muted-foreground">
                      Stale build layers the normal deploy&apos;s own image prune never touches.
                      Reclaimable with a builder prune.
                    </p>
                  )}
                  <div className="flex items-center justify-between">
                    <span className="text-xs text-muted-foreground">Docker images</span>
                    <span className="text-xs font-mono text-muted-foreground">
                      {formatBytes(diskUsage.images_bytes)}
                    </span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-xs text-muted-foreground">Postgres data</span>
                    <span className="text-xs font-mono text-muted-foreground">
                      {formatBytes(diskUsage.postgres_data_bytes)}
                    </span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-xs text-muted-foreground">Deploy backups</span>
                    <span className="text-xs font-mono text-muted-foreground">
                      {formatBytes(diskUsage.backups_bytes)}
                    </span>
                  </div>
                </div>
              )}
            </>
          ) : (
            <p className="text-xs text-muted-foreground">
              Host metrics are unavailable in this runtime.
            </p>
          )}
        </CardContent>
      </Card>

      {/* Container resource load */}
      <Card>
        <CardHeader className="flex-row items-center justify-between space-y-0">
          <CardTitle className="text-sm font-medium text-muted-foreground uppercase tracking-wider">
            Containers
          </CardTitle>
          <Badge
            variant={
              !containerRuntime
                ? 'secondary'
                : anyContainerBad
                  ? 'destructive'
                  : containerFresh
                    ? 'success'
                    : 'warning'
            }
          >
            {!containerRuntime
              ? 'No telemetry'
              : anyContainerBad
                ? 'Unhealthy'
                : containerFresh
                  ? 'Live'
                  : 'Telemetry stale'}
          </Badge>
        </CardHeader>
        <CardContent className="pt-0">
          {containerRuntime ? (
            <div className="space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
                <span>
                  Total {containerRuntime.total_cpu_percent.toFixed(1)}% Docker CPU ·{' '}
                  {formatBytes(containerRuntime.total_memory_used_bytes)} RAM
                </span>
                <span>{timeAgo(containerRuntime.captured_at_ms)}</span>
              </div>
              <p className="text-xs text-muted-foreground">
                Docker CPU uses 100% per fully occupied core.
              </p>
              <div className="overflow-x-auto">
                <table className="w-full min-w-[620px] text-left text-xs">
                  <thead className="text-muted-foreground">
                    <tr className="border-b">
                      <th className="py-2 font-medium">Container</th>
                      <th className="py-2 text-right font-medium">CPU</th>
                      <th className="py-2 text-right font-medium">Memory</th>
                      <th className="py-2 text-right font-medium">PIDs</th>
                      <th className="py-2 text-right font-medium">Restarts</th>
                      <th className="py-2 text-right font-medium">State</th>
                    </tr>
                  </thead>
                  <tbody>
                    {containerRuntime.containers.map((container) => {
                      const severity = containerSeverity(container, containerFresh);
                      const severityClass =
                        severity === 'bad'
                          ? 'text-red-500'
                          : severity === 'stale'
                            ? 'text-amber-500'
                            : severity === 'retired'
                              ? 'text-muted-foreground'
                              : 'text-emerald-500';
                      const label =
                        severity === 'retired'
                          ? 'retired'
                          : container.oom_killed
                            ? 'oom-killed'
                            : container.health === 'none'
                              ? container.status
                              : container.health;
                      return (
                        <tr key={container.name} className="border-b last:border-0">
                          <td className="py-2 font-mono">
                            {containerDisplayName(container.name.replace(/^schurfer-/, ''))}
                          </td>
                          <td className="py-2 text-right font-mono">
                            {container.cpu_percent.toFixed(1)}%
                          </td>
                          <td className="py-2 text-right font-mono">
                            {formatBytes(container.memory_used_bytes)}
                            {container.memory_limit_bytes > 0
                              ? ` / ${formatBytes(container.memory_limit_bytes)}`
                              : ''}
                          </td>
                          <td className="py-2 text-right font-mono">{container.pids}</td>
                          <td className="py-2 text-right font-mono">{container.restart_count}</td>
                          <td className="py-2 text-right">
                            <span className={severityClass}>{label}</span>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">
              Install the host runtime-metrics service to expose sanitized container telemetry.
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
              {marketPipeline ? `${marketPipeline.event_rate_per_sec.toFixed(0)} events/s` : 'n/a'}
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
            <span className="text-sm">Current / window / lifetime-max lag</span>
            <span className="text-xs font-mono text-muted-foreground">
              {marketPipeline
                ? `${marketPipeline.last_lag_ms} / ${marketPipeline.window_max_lag_ms} / ${marketPipeline.max_lag_ms} ms`
                : 'n/a'}
            </span>
          </div>
          <p className="text-xs text-muted-foreground">
            The lifetime-max figure can be a single old outlier; judge current health by the first
            two numbers, not the third.
          </p>
          <div className="flex items-center justify-between">
            <span className="text-sm">Drops (recent / lifetime)</span>
            <span
              className={`text-xs font-mono ${dropsIncreasingNow ? 'text-red-500' : 'text-muted-foreground'}`}
            >
              {marketPipeline
                ? `${dropTracking.recentDelta} / ${pipelineDrops}${
                    dropTracking.recentWindowMs !== null
                      ? ` (last ${Math.round(dropTracking.recentWindowMs / 1000)}s)`
                      : ''
                  }`
                : 'n/a'}
            </span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-sm">Last drop observed</span>
            <span className="text-xs text-muted-foreground">
              {dropTracking.lastDropAtMs
                ? timeAgo(dropTracking.lastDropAtMs)
                : pipelineDrops > 0
                  ? 'none since page loaded (lifetime total predates this session)'
                  : 'none'}
            </span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-sm">Persistence errors (lifetime)</span>
            <span className="text-xs font-mono text-muted-foreground">
              {marketPipeline ? marketPipeline.persist_errors_total : 'n/a'}
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

      {/* Bybit order-flow trial retired 2026-08-06 (see ROADMAP.md): its
          historical results live on the Research page, not here. The
          Redis-backed OrderflowPilotState wire type is kept for backward
          compatibility with anything still reading /api/health directly,
          but this page no longer renders it. */}

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
    </PageShell>
  );
}

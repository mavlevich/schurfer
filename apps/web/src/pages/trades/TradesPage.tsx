import { useState } from 'react';
import { WifiOff, RefreshCw } from 'lucide-react';
import { PageShell } from '@/components/shared/PageShell';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from '@/components/ui/table';
import { useTrades, useTradeStats } from '@/hooks/useTradesData';
import type { Trade, TradeStats } from '@/hooks/useTradesData';

const PAGE_SIZE = 50;

function fmtPrice(n: number): string {
  if (n >= 1000) return `$${n.toLocaleString('en-US', { maximumFractionDigits: 2 })}`;
  if (n >= 1) return `$${n.toFixed(4)}`;
  if (n >= 0.0001) return `$${n.toFixed(6)}`;
  return `$${n.toPrecision(4)}`;
}

function fmtPct(n: number): string {
  return `${n >= 0 ? '+' : ''}${n.toFixed(2)}%`;
}

function fmtUsd(n: number): string {
  return `${n >= 0 ? '+' : '-'}$${Math.abs(n).toFixed(2)}`;
}

function fmtDate(iso: string): string {
  return new Date(iso).toLocaleString('en-US', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
}

function fmtDuration(mins: number): string {
  const total = Math.round(mins); // round first so 119.6m is 2h, not "1h 60m"
  if (total < 60) return `${total}m`;
  const h = Math.floor(total / 60);
  const m = total % 60;
  return m ? `${h}h ${m}m` : `${h}h`;
}

function holdMinutes(t: Trade): number | null {
  if (!t.exit_at) return null;
  return (new Date(t.exit_at).getTime() - new Date(t.entry_at).getTime()) / 60000;
}

function isPaper(t: Trade): boolean {
  return t.mode === 'paper';
}

function strategyVersion(t: Trade): string {
  return t.strategy_version || '—';
}

// notes read like "initial_sl move=-9.1%" / "trailing_stop trail=20% profit=5.8%" /
// "max_hold age=180min"; the first token is the exit reason, the rest are the details.

function ExitReason({ reason }: { reason: string | null }) {
  if (!reason || reason === 'unknown') return <span className="text-muted-foreground">—</span>;
  return (
    <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] font-medium text-foreground">
      {reason.replace(/_/g, ' ')}
    </span>
  );
}

// net and gross are never mixed: display picks the net pair (pct + usd)
// only when accounting_status is complete AND both net fields are present
// together, otherwise it falls back to the gross pair as a whole. ROE is a
// leveraged-return figure and is only meaningful once costs are actually
// modeled, so it is shown for the net case only -- a gross-only ROE would
// overstate real capital efficiency by ignoring fees/funding/slippage
// while looking identically formatted next to a genuine net ROE.
function PnlCell({ trade }: { trade: Trade }) {
  if (trade.status === 'open' || trade.gross_pnl_pct === null || trade.gross_pnl_usd === null) {
    return <span className="text-muted-foreground">—</span>;
  }
  const net =
    trade.accounting_status === 'complete' &&
    trade.net_pnl_pct !== null &&
    trade.net_pnl_usd !== null
      ? { pct: trade.net_pnl_pct, usd: trade.net_pnl_usd }
      : null;
  const displayPct = net ? net.pct : trade.gross_pnl_pct;
  const displayUsd = net ? net.usd : trade.gross_pnl_usd;
  const color = displayPct >= 0 ? 'text-green-500' : 'text-red-500';
  const costs =
    trade.slippage_usd === null ? null : trade.fees_usd + trade.funding_usd + trade.slippage_usd;
  return (
    <div className={`font-mono leading-tight ${color}`}>
      <div>{fmtUsd(displayUsd)}</div>
      <div className="text-xs text-muted-foreground">
        {net ? 'net' : 'gross only'} {fmtPct(displayPct)}
        {net && <> · ROE {fmtPct(net.pct * trade.leverage)}</>}
      </div>
      {net && costs !== null && (
        <div className="text-xs text-muted-foreground">costs {fmtUsd(-costs)}</div>
      )}
    </div>
  );
}

// OriginBadge is deliberately visually distinct from the pre-existing "paper"
// text next to a pump_short token (isPaper below) -- that flag means "this
// specific pump-short trade ran in dry-run mode", a different concept from
// origin="momentum_flow_paper" (this row is not a pump-short trade at all, it
// is momentum_flow's own discovery instrumentation). A momentum_flow_paper
// row must never look like an already-vetted pump-short trade.
function StrategyBadge({ trade }: { trade: Trade }) {
  if (trade.strategy_name === 'momentum_flow') {
    return (
      <Badge
        variant="outline"
        title="Research probe"
        className="border-violet-400/20 bg-violet-400/10 text-violet-400"
      >
        🔭 {trade.strategy_name}
      </Badge>
    );
  }
  if (trade.strategy_name === 'early_momentum') {
    return (
      <Badge variant="outline" className="border-blue-400/20 bg-blue-400/10 text-blue-400">
        ⚡ {trade.strategy_name}
      </Badge>
    );
  }
  if (trade.strategy_name === 'liquidation_cascade') {
    return (
      <Badge variant="outline" className="border-red-400/20 bg-red-400/10 text-red-400">
        💥 {trade.strategy_name}
      </Badge>
    );
  }
  return (
    <Badge variant="outline" className="border-emerald-400/20 bg-emerald-400/10 text-emerald-400">
      {trade.strategy_name || 'pump_short'}
    </Badge>
  );
}

function StatusBadge({ status }: { status: string }) {
  const cls =
    status === 'open'
      ? 'text-sky-400 bg-sky-400/10 border-sky-400/20'
      : 'text-muted-foreground bg-muted border-border';
  return (
    <Badge variant="outline" className={cls}>
      {status}
    </Badge>
  );
}

// Stats come from /api/trades/stats — computed server-side over the whole closed-trade
// set (not just the loaded page). Gross covers legacy and modeled rows; net includes
// only rows whose versioned cost accounting completed.
function StatRow({ stats }: { stats?: TradeStats }) {
  if (!stats || stats.count === 0) return null;
  const {
    count,
    win_rate,
    expectancy,
    avg_win,
    avg_loss,
    profit_factor,
    gross_usd,
    net_count,
    net_win_rate,
    net_expectancy,
    net_profit_factor,
    net_usd,
    legacy_count,
    incomplete_count,
  } = stats;

  const items: { label: string; value: string; cls?: string }[] = [
    { label: 'Closed', value: String(count) },
    { label: 'Gross win rate', value: `${win_rate.toFixed(0)}%` },
    {
      label: 'Gross expectancy',
      value: fmtPct(expectancy),
      cls: expectancy >= 0 ? 'text-green-500' : 'text-red-500',
    },
    { label: 'Gross avg win', value: fmtPct(avg_win), cls: 'text-green-500' },
    { label: 'Gross avg loss', value: fmtPct(avg_loss), cls: 'text-red-500' },
    {
      label: 'Gross PF ($)',
      value: profit_factor === null ? '—' : profit_factor.toFixed(2),
      cls: profit_factor !== null && profit_factor >= 1 ? 'text-green-500' : 'text-red-500',
    },
    {
      label: 'Gross P&L',
      value: fmtUsd(gross_usd),
      cls: gross_usd >= 0 ? 'text-green-500' : 'text-red-500',
    },
  ];

  if (net_count > 0 && net_expectancy !== null && net_usd !== null) {
    items.push(
      {
        label: `Net expectancy (N=${net_count})`,
        value: fmtPct(net_expectancy),
        cls: net_expectancy >= 0 ? 'text-green-500' : 'text-red-500',
      },
      {
        label: 'Net win rate',
        value: net_win_rate === null ? '—' : `${net_win_rate.toFixed(0)}%`,
      },
      {
        label: 'Net PF ($)',
        value: net_profit_factor === null ? '—' : net_profit_factor.toFixed(2),
        cls:
          net_profit_factor !== null && net_profit_factor >= 1 ? 'text-green-500' : 'text-red-500',
      },
      {
        label: 'Net P&L',
        value: fmtUsd(net_usd),
        cls: net_usd >= 0 ? 'text-green-500' : 'text-red-500',
      },
    );
  }

  return (
    <Card>
      <CardContent className="flex flex-wrap items-center gap-x-6 gap-y-2 py-3">
        {items.map((s) => (
          <div key={s.label} className="flex flex-col">
            <span className="text-xs text-muted-foreground">{s.label}</span>
            <span className={`font-mono text-sm ${s.cls ?? ''}`}>{s.value}</span>
          </div>
        ))}
        {net_count === 0 && (
          <span className="ml-auto text-xs text-amber-400/80">
            modeled net collecting · {legacy_count} legacy gross-only
            {incomplete_count > 0 ? ` · ${incomplete_count} incomplete` : ''}
          </span>
        )}
        {net_count > 0 && net_count < 30 && (
          <span className="ml-auto text-xs text-amber-400/80">
            modeled net sample N={net_count} · not statistically meaningful
          </span>
        )}
      </CardContent>
    </Card>
  );
}

export function TradesPage() {
  const [statusFilter, setStatusFilter] = useState('');
  const [exchangeFilter, setExchangeFilter] = useState('');
  const [originFilter, setOriginFilter] = useState<string | ''>('');
  const [strategyFilter, setStrategyFilter] = useState<string | ''>('');
  const [modeFilter, setModeFilter] = useState<string | ''>('');
  const [sideFilter, setSideFilter] = useState<string | ''>('');
  const [offset, setOffset] = useState(0);

  const { data, isError, isFetching, dataUpdatedAt } = useTrades({
    status: statusFilter || undefined,
    exchange: exchangeFilter || undefined,
    origin: originFilter || undefined,
    strategy: strategyFilter || undefined,
    mode: modeFilter || undefined,
    side: sideFilter || undefined,
    limit: PAGE_SIZE,
    offset,
  });
  const { data: stats } = useTradeStats({
    exchange: exchangeFilter || undefined,
    origin: originFilter || undefined,
    strategy: strategyFilter || undefined,
    mode: modeFilter || undefined,
    side: sideFilter || undefined,
  });

  const trades = data?.trades ?? [];
  const total = data?.total ?? 0;
  const totalPages = Math.ceil(total / PAGE_SIZE);
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;
  const lastUpdated = dataUpdatedAt ? new Date(dataUpdatedAt) : null;
  const showEmpty = !isFetching && !isError && data !== undefined && trades.length === 0;

  function handleFilterChange(setter: (v: string) => void) {
    return (e: React.ChangeEvent<HTMLSelectElement>) => {
      setter(e.target.value);
      setOffset(0);
    };
  }

  function handleOriginFilterChange(e: React.ChangeEvent<HTMLSelectElement>) {
    setOriginFilter(e.target.value as string | '');
    setOffset(0);
  }

  return (
    <PageShell width="wide" className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold tracking-tight">Trade Journal</h1>
          <p className="text-sm text-muted-foreground">
            {data ? `${total} trade${total !== 1 ? 's' : ''}` : 'Signal-triggered positions'}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2">
            <select
              value={statusFilter}
              onChange={handleFilterChange(setStatusFilter)}
              className="rounded border border-input bg-background px-2 py-1 text-sm text-foreground"
            >
              <option value="">All status</option>
              <option value="open">Open</option>
              <option value="closed">Closed</option>
            </select>
            <select
              value={exchangeFilter}
              onChange={handleFilterChange(setExchangeFilter)}
              className="rounded border border-input bg-background px-2 py-1 text-sm text-foreground"
            >
              <option value="">All exchanges</option>
              <option value="bybit">Bybit</option>
              <option value="binance">Binance</option>
              <option value="bingx">BingX</option>
              <option value="gate">Gate</option>
              <option value="mexc">MEXC</option>
              <option value="okx">OKX</option>
              <option value="kucoin">KuCoin</option>
            </select>
            <select
              value={strategyFilter}
              onChange={(e) => {
                setStrategyFilter(e.target.value);
                setOffset(0);
              }}
              className="rounded border border-input bg-background px-2 py-1 text-sm text-foreground"
            >
              <option value="">All strategies</option>
              <option value="early_momentum">Early Momentum</option>
              <option value="pump_short">Pump Short</option>
              <option value="liquidation_cascade">Liquidation Cascade</option>
              <option value="momentum_flow">Momentum Flow</option>
            </select>
            <select
              value={modeFilter}
              onChange={(e) => {
                setModeFilter(e.target.value);
                setOffset(0);
              }}
              className="rounded border border-input bg-background px-2 py-1 text-sm text-foreground"
            >
              <option value="">All modes</option>
              <option value="live">Live</option>
              <option value="paper">Paper</option>
            </select>
            <select
              value={sideFilter}
              onChange={(e) => {
                setSideFilter(e.target.value);
                setOffset(0);
              }}
              className="rounded border border-input bg-background px-2 py-1 text-sm text-foreground"
            >
              <option value="">All sides</option>
              <option value="long">Long</option>
              <option value="short">Short</option>
            </select>
            <select
              value={originFilter}
              onChange={handleOriginFilterChange}
              className="rounded border border-input bg-background px-2 py-1 text-sm text-foreground"
            >
              <option value="">All sources</option>
              {/* origin is the combinedTradesCTE literal, not a strategy name --
                  see api-gateway/internal/trades/handler.go's own 'app.trades'/
                  'momentum_flow_paper' tags. */}
              <option value="app.trades">Live/paper execution ledger</option>
              <option value="momentum_flow_paper">Research (momentum_flow)</option>
            </select>
          </div>
          <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
            {isError ? (
              <>
                <WifiOff className="h-3 w-3 text-red-400" />
                <span className="text-red-400">API offline</span>
                {lastUpdated && (
                  <span className="text-muted-foreground/60">
                    · last seen {lastUpdated.toLocaleTimeString('en-US')}
                  </span>
                )}
              </>
            ) : (
              <>
                <RefreshCw className={`h-3 w-3 ${isFetching ? 'animate-spin' : ''}`} />
                {lastUpdated ? `Updated ${lastUpdated.toLocaleTimeString('en-US')}` : 'Loading...'}
              </>
            )}
          </div>
        </div>
      </div>

      {statusFilter !== 'open' && <StatRow stats={stats} />}

      {showEmpty && (
        <Card>
          <CardContent className="py-8 text-center text-sm text-muted-foreground">
            No trades yet. Enable DRY_RUN or AUTO_TRADE to start recording.
          </CardContent>
        </Card>
      )}

      {!showEmpty && (
        <Card>
          <CardContent className="p-0">
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Source</TableHead>
                    <TableHead>Token</TableHead>
                    <TableHead>Exchange</TableHead>
                    <TableHead>Side</TableHead>
                    <TableHead className="text-right">Size · Lev</TableHead>
                    <TableHead className="text-right">Entry → Exit</TableHead>
                    <TableHead className="text-right">P&L</TableHead>
                    <TableHead>Exit</TableHead>
                    <TableHead className="text-right">Held</TableHead>
                    <TableHead>Strategy</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead>Opened</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {trades.map((t) => {
                    const held = holdMinutes(t);
                    return (
                      <TableRow key={t.id}>
                        <TableCell>
                          <StrategyBadge trade={t} />
                        </TableCell>
                        <TableCell className="font-mono font-semibold">
                          {t.symbol.split('/')[0]}
                          {isPaper(t) && (
                            <span className="ml-1.5 text-xs font-normal text-muted-foreground">
                              paper
                            </span>
                          )}
                        </TableCell>
                        <TableCell className="capitalize text-muted-foreground">
                          {t.exchange}
                        </TableCell>
                        <TableCell>
                          <span className={t.side === 'short' ? 'text-red-400' : 'text-green-400'}>
                            {t.side}
                          </span>
                        </TableCell>
                        <TableCell className="whitespace-nowrap text-right font-mono text-muted-foreground">
                          ${t.size_usd.toFixed(0)} · {t.leverage}x
                        </TableCell>
                        <TableCell className="whitespace-nowrap text-right font-mono">
                          {fmtPrice(t.entry_price)}
                          <span className="text-muted-foreground">
                            {' → '}
                            {t.exit_price !== null ? fmtPrice(t.exit_price) : '—'}
                          </span>
                        </TableCell>
                        <TableCell className="text-right">
                          <PnlCell trade={t} />
                        </TableCell>
                        <TableCell>
                          <ExitReason reason={t.exit_reason} />
                        </TableCell>
                        <TableCell className="whitespace-nowrap text-right font-mono text-muted-foreground">
                          {held !== null ? fmtDuration(held) : '—'}
                        </TableCell>
                        <TableCell className="font-mono text-xs text-muted-foreground">
                          {strategyVersion(t)}
                        </TableCell>
                        <TableCell>
                          <StatusBadge status={t.status} />
                        </TableCell>
                        <TableCell className="whitespace-nowrap text-muted-foreground">
                          {fmtDate(t.entry_at)}
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
          </CardContent>
        </Card>
      )}

      {totalPages > 1 && (
        <div className="flex items-center justify-between text-sm text-muted-foreground">
          <span>
            Page {currentPage} of {totalPages}
          </span>
          <div className="flex gap-2">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setOffset((p) => Math.max(0, p - PAGE_SIZE))}
              disabled={offset === 0}
              className="border"
            >
              Prev
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setOffset((p) => p + PAGE_SIZE)}
              disabled={offset + PAGE_SIZE >= total}
              className="border"
            >
              Next
            </Button>
          </div>
        </div>
      )}
    </PageShell>
  );
}

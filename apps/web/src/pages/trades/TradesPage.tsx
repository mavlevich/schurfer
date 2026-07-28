import { useState } from 'react';
import { WifiOff, RefreshCw } from 'lucide-react';
import { Nav } from '@/components/Nav';
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
  return t.setup_context?.paper === true;
}

function strategyVersion(t: Trade): string {
  const v = t.setup_context?.strategy_version;
  return typeof v === 'string' ? v : '—';
}

// notes read like "initial_sl move=-9.1%" / "trailing_stop trail=20% profit=5.8%" /
// "max_hold age=180min"; the first token is the exit reason, the rest are the details.
const EXIT_STYLES: Record<string, string> = {
  initial_sl: 'text-red-400 bg-red-400/10 border-red-400/20',
  trailing_stop: 'text-sky-400 bg-sky-400/10 border-sky-400/20',
  max_hold: 'text-amber-400 bg-amber-400/10 border-amber-400/20',
};

function ExitReason({ notes }: { notes: string | null }) {
  if (!notes) return <span className="text-muted-foreground">—</span>;
  const reason = notes.split(' ')[0];
  const cls = EXIT_STYLES[reason] ?? 'text-muted-foreground bg-muted border-border';
  return (
    <span
      title={notes}
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium ${cls}`}
    >
      {reason}
    </span>
  );
}

function PnlCell({ trade }: { trade: Trade }) {
  if (trade.status === 'open' || trade.gross_pnl_pct === null) {
    return <span className="text-muted-foreground">—</span>;
  }
  const displayPct = trade.net_pnl_pct ?? trade.gross_pnl_pct;
  const displayUsd = trade.net_pnl_usd ?? trade.gross_pnl_usd;
  const color = displayPct >= 0 ? 'text-green-500' : 'text-red-500';
  const roe = displayPct * trade.leverage;
  const modeled = trade.accounting_status === 'complete';
  const costs =
    trade.slippage_usd === null ? null : trade.fees_usd + trade.funding_usd + trade.slippage_usd;
  return (
    <div className={`font-mono leading-tight ${color}`}>
      <div>{displayUsd !== null ? fmtUsd(displayUsd) : fmtPct(displayPct)}</div>
      <div className="text-xs text-muted-foreground">
        {modeled ? 'net' : 'gross only'} {fmtPct(displayPct)} · ROE {fmtPct(roe)}
      </div>
      {modeled && costs !== null && (
        <div className="text-xs text-muted-foreground">costs {fmtUsd(-costs)}</div>
      )}
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const cls =
    status === 'open'
      ? 'text-sky-400 bg-sky-400/10 border-sky-400/20'
      : 'text-muted-foreground bg-muted border-border';
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium ${cls}`}
    >
      {status}
    </span>
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
  const [offset, setOffset] = useState(0);

  const { data, isError, isFetching, dataUpdatedAt } = useTrades({
    status: statusFilter || undefined,
    exchange: exchangeFilter || undefined,
    limit: PAGE_SIZE,
    offset,
  });
  const { data: stats } = useTradeStats({ exchange: exchangeFilter || undefined });

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

  return (
    <div className="min-h-screen bg-background">
      <Nav />
      <div className="mx-auto max-w-6xl space-y-4 p-4 md:p-8">
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
                  {lastUpdated
                    ? `Updated ${lastUpdated.toLocaleTimeString('en-US')}`
                    : 'Loading...'}
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
                            <span
                              className={t.side === 'short' ? 'text-red-400' : 'text-green-400'}
                            >
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
                            <ExitReason notes={t.notes} />
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
              <button
                onClick={() => setOffset((p) => Math.max(0, p - PAGE_SIZE))}
                disabled={offset === 0}
                className="rounded border px-3 py-1 hover:bg-muted/50 disabled:opacity-40"
              >
                Prev
              </button>
              <button
                onClick={() => setOffset((p) => p + PAGE_SIZE)}
                disabled={offset + PAGE_SIZE >= total}
                className="rounded border px-3 py-1 hover:bg-muted/50 disabled:opacity-40"
              >
                Next
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

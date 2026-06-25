import { useState } from 'react';
import { WifiOff, RefreshCw } from 'lucide-react';
import { Nav } from '@/components/Nav';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from '@/components/ui/table';
import { useTrades } from '@/hooks/useTradesData';
import type { Trade } from '@/hooks/useTradesData';

const PAGE_SIZE = 50;

function fmtPrice(n: number): string {
  if (n >= 1000) return `$${n.toLocaleString('en-US', { maximumFractionDigits: 2 })}`;
  if (n >= 1) return `$${n.toFixed(4)}`;
  if (n >= 0.0001) return `$${n.toFixed(6)}`;
  return `$${n.toPrecision(4)}`;
}

function fmtPct(n: number): string {
  const sign = n >= 0 ? '+' : '';
  return `${sign}${n.toFixed(2)}%`;
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

function isPaper(trade: Trade): boolean {
  return trade.setup_context?.paper === true;
}

function PnlCell({ trade }: { trade: Trade }) {
  if (trade.status === 'open' || trade.pnl_pct === null) {
    return <span className="text-muted-foreground">—</span>;
  }
  return (
    <span className={trade.pnl_pct >= 0 ? 'text-green-500' : 'text-red-500'}>
      {fmtPct(trade.pnl_pct)}
    </span>
  );
}

function OutcomeBadge({ label }: { label: string | null }) {
  if (!label) return <span className="text-muted-foreground">—</span>;
  const variant = label === 'win' ? 'default' : label === 'loss' ? 'destructive' : 'secondary';
  return <Badge variant={variant}>{label}</Badge>;
}

function StatusBadge({ status }: { status: string }) {
  if (status === 'open') return <Badge variant="outline">open</Badge>;
  if (status === 'closed') return <Badge variant="secondary">closed</Badge>;
  if (status === 'cancelled') return <Badge variant="secondary">cancelled</Badge>;
  return <Badge variant="secondary">{status}</Badge>;
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
      <div className="mx-auto max-w-6xl p-4 md:p-8 space-y-4">
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
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Symbol</TableHead>
                    <TableHead>Exchange</TableHead>
                    <TableHead>Side</TableHead>
                    <TableHead className="text-right">Size</TableHead>
                    <TableHead className="text-right">Lev</TableHead>
                    <TableHead className="text-right">Entry</TableHead>
                    <TableHead className="text-right">Exit</TableHead>
                    <TableHead className="text-right">P&L</TableHead>
                    <TableHead>Outcome</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead>Opened</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {trades.map((t) => (
                    <TableRow key={t.id}>
                      <TableCell className="font-mono font-semibold">
                        {t.symbol.split('/')[0]}
                        {isPaper(t) && (
                          <span className="ml-1.5 text-xs font-normal text-muted-foreground">
                            paper
                          </span>
                        )}
                      </TableCell>
                      <TableCell className="capitalize">{t.exchange}</TableCell>
                      <TableCell>
                        <span className={t.side === 'short' ? 'text-red-400' : 'text-green-400'}>
                          {t.side}
                        </span>
                      </TableCell>
                      <TableCell className="text-right">${t.size_usd.toFixed(0)}</TableCell>
                      <TableCell className="text-right">{t.leverage}x</TableCell>
                      <TableCell className="text-right font-mono">
                        {fmtPrice(t.entry_price)}
                      </TableCell>
                      <TableCell className="text-right font-mono">
                        {t.exit_price !== null ? (
                          fmtPrice(t.exit_price)
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </TableCell>
                      <TableCell className="text-right font-mono">
                        <PnlCell trade={t} />
                      </TableCell>
                      <TableCell>
                        <OutcomeBadge label={t.outcome_label} />
                      </TableCell>
                      <TableCell>
                        <StatusBadge status={t.status} />
                      </TableCell>
                      <TableCell className="whitespace-nowrap text-muted-foreground">
                        {fmtDate(t.entry_at)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
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
                className="rounded border px-3 py-1 disabled:opacity-40 hover:bg-muted/50"
              >
                Prev
              </button>
              <button
                onClick={() => setOffset((p) => p + PAGE_SIZE)}
                disabled={offset + PAGE_SIZE >= total}
                className="rounded border px-3 py-1 disabled:opacity-40 hover:bg-muted/50"
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

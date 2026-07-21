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
import { useDecisions } from '@/hooks/useDecisionsData';
import type { Decision } from '@/hooks/useDecisionsData';

const PAGE_SIZE = 50;

const ACTION_STYLES: Record<string, string> = {
  opened: 'text-green-400 bg-green-400/10 border-green-400/20',
  opened_dry_run: 'text-sky-400 bg-sky-400/10 border-sky-400/20',
  skipped: 'text-muted-foreground bg-muted border-border',
};

const ACTION_LABELS: Record<string, string> = {
  opened: 'opened',
  opened_dry_run: 'dry run',
  skipped: 'skipped',
};

function ActionBadge({ action }: { action: string }) {
  const cls = ACTION_STYLES[action] ?? ACTION_STYLES['skipped'];
  const label = ACTION_LABELS[action] ?? action;
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium ${cls}`}
    >
      {label}
    </span>
  );
}

function fmtTs(iso: string): string {
  return new Date(iso).toLocaleString('en-US', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
}

function ScoreCell({ d }: { d: Decision }) {
  if (d.score === null) return <span className="text-muted-foreground">—</span>;
  return <span className="font-mono">{d.score}</span>;
}

function PumpCell({ d }: { d: Decision }) {
  if (d.pump_pct === null) return <span className="text-muted-foreground">—</span>;
  return (
    <span className="font-mono">
      {d.pump_pct >= 0 ? '+' : ''}
      {d.pump_pct.toFixed(1)}%
    </span>
  );
}

// Alt prices span a huge range (sub-cent memecoins to four-figure majors), so
// format by significant digits instead of a fixed scale, and avoid exponential.
function fmtPrice(p: number): string {
  return new Intl.NumberFormat('en-US', { maximumSignificantDigits: 6 }).format(p);
}

function PriceCell({ d }: { d: Decision }) {
  if (d.price === null) return <span className="text-muted-foreground">—</span>;
  return <span className="font-mono">${fmtPrice(d.price)}</span>;
}

export function DecisionsPage() {
  const [baseFilter, setBaseFilter] = useState('');
  const [actionFilter, setActionFilter] = useState('');
  const [offset, setOffset] = useState(0);

  const { data, isError, isFetching, dataUpdatedAt } = useDecisions({
    base: baseFilter.trim().toUpperCase() || undefined,
    action: actionFilter || undefined,
    limit: PAGE_SIZE,
    offset,
  });

  const decisions = data?.decisions ?? [];
  const total = data?.total ?? 0;
  const totalPages = Math.ceil(total / PAGE_SIZE);
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;
  const lastUpdated = dataUpdatedAt ? new Date(dataUpdatedAt) : null;
  const showEmpty = !isFetching && !isError && data !== undefined && decisions.length === 0;

  function handleFilterChange(setter: (v: string) => void) {
    return (e: React.ChangeEvent<HTMLSelectElement | HTMLInputElement>) => {
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
            <h1 className="text-xl font-bold tracking-tight">Decision Log</h1>
            <p className="text-sm text-muted-foreground">
              {data ? `${total} decision${total !== 1 ? 's' : ''}` : 'Every signal evaluation'}
            </p>
          </div>
          <div className="flex items-center gap-3">
            <div className="flex items-center gap-2">
              <input
                type="text"
                placeholder="Token"
                value={baseFilter}
                onChange={handleFilterChange(setBaseFilter)}
                className="w-24 rounded border border-input bg-background px-2 py-1 text-sm text-foreground placeholder:text-muted-foreground uppercase"
              />
              <select
                value={actionFilter}
                onChange={handleFilterChange(setActionFilter)}
                className="rounded border border-input bg-background px-2 py-1 text-sm text-foreground"
              >
                <option value="">All actions</option>
                <option value="opened">Opened</option>
                <option value="opened_dry_run">Dry run</option>
                <option value="skipped">Skipped</option>
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
              No decisions recorded yet. Signal trader writes here on every evaluation.
            </CardContent>
          </Card>
        )}

        {!showEmpty && (
          <Card>
            <CardContent className="p-0">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Time</TableHead>
                    <TableHead>Token</TableHead>
                    <TableHead>Exchange</TableHead>
                    <TableHead>Action</TableHead>
                    <TableHead>Reason</TableHead>
                    <TableHead className="text-right">Score</TableHead>
                    <TableHead className="text-right">Pump</TableHead>
                    <TableHead className="text-right">Price</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {decisions.map((d) => (
                    <TableRow key={d.id}>
                      <TableCell className="whitespace-nowrap text-muted-foreground text-xs">
                        {fmtTs(d.ts)}
                      </TableCell>
                      <TableCell className="font-mono font-semibold">{d.base}</TableCell>
                      <TableCell className="capitalize text-muted-foreground">
                        {d.exchange || '—'}
                      </TableCell>
                      <TableCell>
                        <ActionBadge action={d.action} />
                      </TableCell>
                      <TableCell
                        className="text-xs text-muted-foreground max-w-[280px] truncate"
                        title={d.reason}
                      >
                        {d.reason}
                      </TableCell>
                      <TableCell className="text-right">
                        <ScoreCell d={d} />
                      </TableCell>
                      <TableCell className="text-right">
                        <PumpCell d={d} />
                      </TableCell>
                      <TableCell className="text-right">
                        <PriceCell d={d} />
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

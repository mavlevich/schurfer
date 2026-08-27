import { useMemo, useState } from 'react';
import { Link } from 'react-router';
import { RefreshCw, WifiOff } from 'lucide-react';
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  SortingState,
} from '@tanstack/react-table';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { DataTableColumnHeader } from '@/components/ui/data-table-column-header';
import { usePumps, usePumpsHistory } from '@/hooks/usePumpsData';
import { formatVolume, summarizeVolume, volumeRank } from '../volume';
import type { ExchangeEntry } from '../types';
import { fmtPct, pctColor, timeAgo, fmtPrice } from '@/lib/formatters';
import { Percent } from '@/components/ui/domain/Percent';
import { Price } from '@/components/ui/domain/Price';
import { TimeFormatted } from '@/components/ui/domain/TimeFormatted';

function topPrice(exchanges: ExchangeEntry[]): number {
  let best = 0;
  let bestVol = -1;
  for (const e of exchanges) {
    const p = parseFloat(e.price);
    const volume = volumeRank(e.volume_24h_usd);
    if (p > 0 && volume > bestVol) {
      best = p;
      bestVol = volume;
    }
  }
  return best;
}

function high24hPct(exchanges: ExchangeEntry[]): number {
  let max = 0;
  for (const e of exchanges) {
    const price = parseFloat(e.price);
    const high = parseFloat(e.high_24h);
    if (price > 0 && high > 0 && e.change_pct > -100) {
      const open = price / (1 + e.change_pct / 100);
      const pct = ((high - open) / open) * 100;
      if (pct > max) max = pct;
    }
  }
  return max;
}

type UnifiedPumpRow = {
  id: string;
  base: string;
  isLive: boolean;
  observedPeakPct: number;
  rollingHighPct: number;
  nowPct: number;
  price: number;
  exchanges: ExchangeEntry[];
  volume: { value: number; partial: boolean };
  firstSeenAt?: number;
  lastSeenAt?: number;
};

const columnHelper = createColumnHelper<UnifiedPumpRow>();

const columns = [
  columnHelper.accessor('base', {
    header: ({ column }) => <DataTableColumnHeader column={column} title="Token" />,
    cell: ({ row }) => (
      <div>
        <Link
          to={`/pumps/${row.original.base}`}
          className="font-mono font-semibold hover:text-primary transition-colors"
        >
          {row.original.base}
        </Link>
        {!row.original.isLive && row.original.firstSeenAt && row.original.lastSeenAt && (
          <div className="text-xs text-muted-foreground font-normal leading-tight mt-0.5">
            on radar{' '}
            <TimeFormatted value={row.original.firstSeenAt} format="relative" tabular={false} /> ·
            last seen{' '}
            <TimeFormatted value={row.original.lastSeenAt} format="relative" tabular={false} />
          </div>
        )}
      </div>
    ),
  }),
  columnHelper.accessor('observedPeakPct', {
    header: ({ column }) => (
      <DataTableColumnHeader column={column} title="Observed Peak" className="justify-end" />
    ),
    cell: ({ getValue, row }) => (
      <div className="text-right">
        <Percent value={getValue()} className={row.original.isLive ? 'font-bold' : ''} />
      </div>
    ),
  }),
  columnHelper.accessor('rollingHighPct', {
    header: ({ column }) => (
      <DataTableColumnHeader column={column} title="24h High" className="justify-end" />
    ),
    cell: ({ getValue }) => (
      <div className="text-right">
        <Percent value={getValue()} />
      </div>
    ),
  }),
  columnHelper.accessor('nowPct', {
    header: ({ column }) => (
      <DataTableColumnHeader column={column} title="Now" className="justify-end" />
    ),
    cell: ({ getValue, row }) => (
      <div className="text-right">
        <Percent value={getValue()} theme={row.original.isLive ? 'pump' : 'none'} />
      </div>
    ),
  }),
  columnHelper.accessor('price', {
    header: ({ column }) => (
      <DataTableColumnHeader column={column} title="Price" className="justify-end" />
    ),
    cell: ({ getValue }) => (
      <div className="text-right text-sm">
        <Price value={getValue()} />
      </div>
    ),
  }),
  columnHelper.accessor('exchanges', {
    header: ({ column }) => <DataTableColumnHeader column={column} title="Exchanges" />,
    enableSorting: false,
    cell: ({ getValue, row }) => (
      <div className="flex flex-wrap gap-1">
        {getValue().map((e) => (
          <Badge
            key={e.exchange}
            variant={row.original.isLive ? 'secondary' : 'outline'}
            className={`text-xs font-normal ${!row.original.isLive ? 'opacity-60' : ''}`}
          >
            {e.exchange} {row.original.isLive && fmtPct(e.change_pct)}
          </Badge>
        ))}
      </div>
    ),
  }),
  columnHelper.accessor('volume.value', {
    id: 'volume',
    header: ({ column }) => (
      <DataTableColumnHeader column={column} title="Volume" className="justify-end" />
    ),
    cell: ({ row }) => (
      <div className="text-right font-mono text-muted-foreground tabular-nums">
        {formatVolume(row.original.volume)}
      </div>
    ),
  }),
];

export function PumpTable() {
  const { data, isError, isFetching, dataUpdatedAt } = usePumps();
  const { data: history = [] } = usePumpsHistory();
  const [sorting, setSorting] = useState<SortingState>([{ id: 'observedPeakPct', desc: true }]);

  const unifiedData = useMemo(() => {
    const pumps = data?.pumps ?? [];
    const liveSet = new Set(pumps.map((p) => p.base));
    const historical = history.filter((h) => !liveSet.has(h.base));

    const rows: UnifiedPumpRow[] = [];

    for (const p of pumps) {
      const hist = history.find((h) => h.base === p.base && h.is_live);
      rows.push({
        id: p.pump_event_id,
        base: p.base,
        isLive: true,
        observedPeakPct: Math.max(hist?.observed_peak_pct ?? 0, p.max_change_pct),
        rollingHighPct: Math.max(hist?.exchange_24h_high_pct ?? 0, high24hPct(p.exchanges)),
        nowPct: p.max_change_pct,
        price: topPrice(p.exchanges),
        exchanges: p.exchanges,
        volume: summarizeVolume(p.exchanges),
      });
    }

    for (const h of historical) {
      rows.push({
        id: `${h.base}-${h.first_seen_at}`,
        base: h.base,
        isLive: false,
        observedPeakPct: h.observed_peak_pct,
        rollingHighPct: h.exchange_24h_high_pct,
        nowPct: h.last_pct,
        price: topPrice(h.exchanges),
        exchanges: h.exchanges,
        volume: summarizeVolume(h.exchanges),
        firstSeenAt: h.first_seen_at,
        lastSeenAt: h.last_seen_at,
      });
    }

    // Sort manually by isLive first, then by observedPeakPct
    return rows.sort((a, b) => {
      if (a.isLive !== b.isLive) return a.isLive ? -1 : 1;
      return b.observedPeakPct - a.observedPeakPct;
    });
  }, [data, history]);

  const table = useReactTable({
    data: unifiedData,
    columns,
    getCoreRowModel: getCoreRowModel(),
    onSortingChange: setSorting,
    getSortedRowModel: getSortedRowModel(),
    state: { sorting },
  });

  const pumpsCount = data?.pumps?.length ?? 0;
  const historyCount = history.filter(
    (h) => !new Set((data?.pumps ?? []).map((p) => p.base)).has(h.base),
  ).length;
  const hasAny = pumpsCount > 0 || historyCount > 0;
  const lastUpdated = dataUpdatedAt ? new Date(dataUpdatedAt) : null;
  const showEmpty = !isFetching && !isError && data !== undefined && !hasAny;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          Linear perps with 24h change ≥ {data?.min_change_pct ?? 30}% across all exchanges
        </p>
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
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

      {!data && !isError && <p className="text-sm text-muted-foreground">Fetching pumps...</p>}

      {showEmpty && (
        <Card>
          <CardContent className="py-8 text-center text-sm text-muted-foreground">
            No pumps above {data?.min_change_pct ?? 30}% right now.
          </CardContent>
        </Card>
      )}

      {hasAny && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground uppercase tracking-wider">
              {pumpsCount > 0
                ? `${pumpsCount} active · ${historyCount} in 24h history`
                : `${historyCount} in 24h history`}
            </CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            <div className="overflow-x-auto">
              <Table className="min-w-[760px]">
                <TableHeader>
                  {table.getHeaderGroups().map((headerGroup) => (
                    <TableRow
                      key={headerGroup.id}
                      className="border-b text-xs text-muted-foreground hover:bg-transparent"
                    >
                      {headerGroup.headers.map((header) => (
                        <TableHead key={header.id} className="px-4 py-2">
                          {header.isPlaceholder
                            ? null
                            : flexRender(header.column.columnDef.header, header.getContext())}
                        </TableHead>
                      ))}
                    </TableRow>
                  ))}
                </TableHeader>
                <TableBody>
                  {table.getRowModel().rows?.length ? (
                    table.getRowModel().rows.map((row) => (
                      <TableRow
                        key={row.id}
                        data-state={row.getIsSelected() && 'selected'}
                        className={`border-b last:border-0 hover:bg-accent/30 transition-colors ${
                          !row.original.isLive ? 'opacity-50 hover:opacity-80' : ''
                        }`}
                      >
                        {row.getVisibleCells().map((cell) => (
                          <TableCell key={cell.id} className="px-4 py-3">
                            {flexRender(cell.column.columnDef.cell, cell.getContext())}
                          </TableCell>
                        ))}
                      </TableRow>
                    ))
                  ) : (
                    <TableRow>
                      <TableCell
                        colSpan={columns.length}
                        className="h-24 text-center text-muted-foreground"
                      >
                        No results.
                      </TableCell>
                    </TableRow>
                  )}
                </TableBody>
              </Table>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

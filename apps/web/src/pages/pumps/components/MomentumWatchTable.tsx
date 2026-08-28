import { useState } from 'react';
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
import { useMomentumWatch } from '@/hooks/usePumpsData';
import { fmtUsdCompact } from '@/lib/formatters';
import { Percent } from '@/components/ui/domain/Percent';
import { TimeFormatted } from '@/components/ui/domain/TimeFormatted';
import type { MomentumWatchEntry } from '../types';

const columnHelper = createColumnHelper<MomentumWatchEntry>();

const columns = [
  columnHelper.accessor('symbol', {
    header: ({ column }) => <DataTableColumnHeader column={column} title="Symbol" />,
    cell: ({ getValue }) => <div className="font-mono font-semibold">{getValue()}</div>,
  }),
  columnHelper.accessor('exchange', {
    header: ({ column }) => <DataTableColumnHeader column={column} title="Exchange" />,
    cell: ({ getValue }) => (
      <Badge variant="secondary" className="text-xs font-normal">
        {getValue()}
      </Badge>
    ),
  }),
  columnHelper.accessor('first_watch_at', {
    header: ({ column }) => (
      <DataTableColumnHeader column={column} title="First watch" className="justify-end" />
    ),
    cell: ({ getValue }) => (
      <div className="text-right">
        <TimeFormatted value={getValue()} format="relative" />
      </div>
    ),
  }),
  columnHelper.accessor('clear_streak', {
    header: ({ column }) => (
      <DataTableColumnHeader column={column} title="Clear streak" className="justify-end" />
    ),
    cell: ({ getValue }) => (
      <div className="text-right font-mono text-muted-foreground tabular-nums">{getValue()}</div>
    ),
  }),
  columnHelper.accessor('price_return_60m_pct', {
    header: ({ column }) => (
      <DataTableColumnHeader column={column} title="60m return" className="justify-end" />
    ),
    cell: ({ getValue }) => (
      <div className="text-right">
        <Percent value={getValue()} theme="signed" className="font-bold" />
      </div>
    ),
  }),
  columnHelper.accessor('price_return_15m_pct', {
    header: ({ column }) => (
      <DataTableColumnHeader column={column} title="15m return" className="justify-end" />
    ),
    cell: ({ getValue }) => (
      <div className="text-right">
        <Percent value={getValue()} theme="signed" />
      </div>
    ),
  }),
  columnHelper.accessor('oi_growth_60m_pct', {
    header: ({ column }) => (
      <DataTableColumnHeader column={column} title="OI growth 60m" className="justify-end" />
    ),
    cell: ({ getValue }) => (
      <div className="text-right">
        <Percent value={getValue()} theme="signed" />
      </div>
    ),
  }),
  columnHelper.accessor('buy_imbalance_15m', {
    header: ({ column }) => (
      <DataTableColumnHeader column={column} title="Buy imbalance 15m" className="justify-end" />
    ),
    cell: ({ getValue }) => (
      <div className="text-right font-mono text-muted-foreground tabular-nums">
        {getValue() === null ? '—' : getValue()!.toFixed(2)}
      </div>
    ),
  }),
  columnHelper.accessor('flow_notional_15m_usd', {
    header: ({ column }) => (
      <DataTableColumnHeader column={column} title="Flow 15m" className="justify-end" />
    ),
    cell: ({ getValue }) => (
      <div className="text-right font-mono text-muted-foreground tabular-nums">
        {fmtUsdCompact(getValue())}
      </div>
    ),
  }),
];

export function MomentumWatchTable() {
  const { data, isError, isFetching, dataUpdatedAt } = useMomentumWatch();
  const watch = data?.watch ?? [];
  const lastUpdated = dataUpdatedAt ? new Date(dataUpdatedAt) : null;
  const showEmpty = !isFetching && !isError && data !== undefined && watch.length === 0;

  const [sorting, setSorting] = useState<SortingState>([{ id: 'first_watch_at', desc: true }]);

  const table = useReactTable({
    data: watch,
    columns,
    getCoreRowModel: getCoreRowModel(),
    onSortingChange: setSorting,
    getSortedRowModel: getSortedRowModel(),
    state: { sorting },
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          Prospective longs: symbols with an active momentum_flow WATCH episode (60m price return,
          OI growth, order-flow imbalance), not a 24h % change scan.
        </p>
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          {isError ? (
            <>
              <WifiOff className="h-3 w-3 text-red-400" />
              <span className="text-red-400">API offline</span>
            </>
          ) : (
            <>
              <RefreshCw className={`h-3 w-3 ${isFetching ? 'animate-spin' : ''}`} />
              {lastUpdated ? `Updated ${lastUpdated.toLocaleTimeString('en-US')}` : 'Loading...'}
            </>
          )}
        </div>
      </div>

      {showEmpty && (
        <Card>
          <CardContent className="py-8 text-center text-sm text-muted-foreground">
            No active momentum_flow WATCH episodes right now.
          </CardContent>
        </Card>
      )}

      {watch.length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground uppercase tracking-wider">
              {watch.length} active
            </CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            <div className="overflow-x-auto">
              <Table className="min-w-[820px]">
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
                        className="border-b last:border-0 hover:bg-accent/30 transition-colors"
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

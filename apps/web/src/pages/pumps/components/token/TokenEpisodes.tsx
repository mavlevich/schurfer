import { useState } from 'react';
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  SortingState,
} from '@tanstack/react-table';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { DataTableColumnHeader } from '@/components/ui/data-table-column-header';
import { useTokenEpisodes } from '@/hooks/useTokenData';
import { Percent } from '@/components/ui/domain/Percent';
import { TimeFormatted } from '@/components/ui/domain/TimeFormatted';
import type { PumpHistoryEntry } from '../../types';

const columnHelper = createColumnHelper<PumpHistoryEntry>();

const columns = [
  columnHelper.accessor('episode', {
    header: ({ column }) => <DataTableColumnHeader column={column} title="#" />,
    cell: ({ getValue }) => <div className="font-mono text-muted-foreground">{getValue()}</div>,
  }),
  columnHelper.accessor('first_seen_at', {
    header: ({ column }) => <DataTableColumnHeader column={column} title="First seen" />,
    cell: ({ getValue }) => <TimeFormatted value={getValue()} />,
  }),
  columnHelper.accessor('closed_at', {
    header: ({ column }) => <DataTableColumnHeader column={column} title="Ended" />,
    cell: ({ getValue }) => <TimeFormatted value={getValue()} />,
  }),
  columnHelper.accessor('observed_peak_pct', {
    header: ({ column }) => (
      <DataTableColumnHeader column={column} title="Observed peak" className="justify-end" />
    ),
    cell: ({ getValue }) => (
      <div className="text-right">
        <Percent value={getValue()} className="font-bold" />
      </div>
    ),
  }),
  columnHelper.accessor('exchange_24h_high_pct', {
    header: ({ column }) => (
      <DataTableColumnHeader column={column} title="24h high" className="justify-end" />
    ),
    cell: ({ getValue }) => (
      <div className="text-right">
        <Percent value={getValue()} />
      </div>
    ),
  }),
  columnHelper.accessor('retrace_pct', {
    header: ({ column }) => (
      <DataTableColumnHeader column={column} title="24h retrace" className="justify-end" />
    ),
    cell: ({ getValue }) => (
      <div className="text-right">
        <Percent value={getValue()} theme="none" />
      </div>
    ),
  }),
  columnHelper.accessor('is_live', {
    header: ({ column }) => (
      <DataTableColumnHeader column={column} title="Status" className="justify-center" />
    ),
    cell: ({ getValue }) => (
      <div className="text-center">
        {getValue() ? (
          <span className="text-xs font-medium text-green-400">LIVE</span>
        ) : (
          <span className="text-xs text-muted-foreground">closed</span>
        )}
      </div>
    ),
  }),
];

export function TokenEpisodes({ base }: { base: string }) {
  const { data: episodes, isPending, isError } = useTokenEpisodes(base);
  const [sorting, setSorting] = useState<SortingState>([{ id: 'episode', desc: true }]);

  const table = useReactTable({
    data: episodes ?? [],
    columns,
    getCoreRowModel: getCoreRowModel(),
    onSortingChange: setSorting,
    getSortedRowModel: getSortedRowModel(),
    state: { sorting },
  });

  if (isPending) {
    return <Skeleton className="h-[250px] w-full" />;
  }
  if (isError || !episodes || episodes.length === 0) {
    return null;
  }

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground uppercase tracking-wider">
          Pump episodes · {episodes.length} recorded
        </CardTitle>
      </CardHeader>
      <CardContent className="p-0">
        <div className="overflow-x-auto">
          <Table className="min-w-[640px]">
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
  );
}

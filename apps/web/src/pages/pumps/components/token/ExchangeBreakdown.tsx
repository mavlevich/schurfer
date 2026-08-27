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
import { useToken } from '@/hooks/useTokenData';
import { formatVolume } from '../../volume';
import { Percent } from '@/components/ui/domain/Percent';
import { Price } from '@/components/ui/domain/Price';
import type { ExchangeEntry } from '../../types';

const columnHelper = createColumnHelper<ExchangeEntry>();

const columns = [
  columnHelper.accessor('exchange', {
    header: ({ column }) => <DataTableColumnHeader column={column} title="Exchange" />,
    cell: ({ getValue }) => <div className="font-medium capitalize">{getValue()}</div>,
  }),
  columnHelper.accessor('change_pct', {
    header: ({ column }) => (
      <DataTableColumnHeader column={column} title="24h %" className="justify-end" />
    ),
    cell: ({ getValue }) => (
      <div className="text-right">
        <Percent value={getValue()} className="font-bold" />
      </div>
    ),
  }),
  columnHelper.accessor((row) => parseFloat(row.price) || 0, {
    id: 'price',
    header: ({ column }) => (
      <DataTableColumnHeader column={column} title="Price" className="justify-end" />
    ),
    cell: ({ getValue }) => (
      <div className="text-right">
        <Price value={getValue()} />
      </div>
    ),
  }),
  columnHelper.accessor((row) => parseFloat(row.high_24h) || 0, {
    id: 'high_24h',
    header: ({ column }) => (
      <DataTableColumnHeader column={column} title="24h High" className="justify-end" />
    ),
    cell: ({ getValue }) => (
      <div className="text-right">
        <Price value={getValue()} />
      </div>
    ),
  }),
  columnHelper.accessor('volume_24h_usd', {
    header: ({ column }) => (
      <DataTableColumnHeader column={column} title="Volume" className="justify-end" />
    ),
    cell: ({ getValue }) => (
      <div className="text-right font-mono text-muted-foreground tabular-nums">
        {formatVolume({ value: getValue(), partial: false })}
      </div>
    ),
  }),
];

export function ExchangeBreakdown({ base }: { base: string }) {
  const { data: pump, isPending, isError } = useToken(base);
  const [sorting, setSorting] = useState<SortingState>([{ id: 'volume_24h_usd', desc: true }]);

  const table = useReactTable({
    data: pump?.exchanges ?? [],
    columns,
    getCoreRowModel: getCoreRowModel(),
    onSortingChange: setSorting,
    getSortedRowModel: getSortedRowModel(),
    state: { sorting },
  });

  if (isPending) {
    return <Skeleton className="h-[200px] w-full" />;
  }
  if (isError || !pump || pump.exchanges.length === 0) {
    return null;
  }

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground uppercase tracking-wider">
          Exchange breakdown
        </CardTitle>
      </CardHeader>
      <CardContent className="p-0">
        <div className="overflow-x-auto">
          <Table className="min-w-[480px]">
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

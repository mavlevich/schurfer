import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { useToken } from '@/hooks/useTokenData';
import { fmtPct, pctColor, fmtPrice } from '@/lib/formatters';
import { formatVolume } from '../../volume';

export function ExchangeBreakdown({ base }: { base: string }) {
  const { data: pump, isPending, isError } = useToken(base);

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
          <table className="w-full min-w-[480px] text-sm">
            <thead>
              <tr className="border-b text-xs text-muted-foreground">
                <th className="px-4 py-2 text-left">Exchange</th>
                <th className="px-4 py-2 text-right">24h %</th>
                <th className="px-4 py-2 text-right">Price</th>
                <th className="px-4 py-2 text-right">24h High</th>
                <th className="px-4 py-2 text-right">Volume</th>
              </tr>
            </thead>
            <tbody>
              {pump.exchanges.map((e) => (
                <tr key={e.exchange} className="border-b last:border-0">
                  <td className="px-4 py-3 font-medium capitalize">{e.exchange}</td>
                  <td
                    className={`px-4 py-3 text-right font-mono font-bold ${pctColor(e.change_pct)}`}
                  >
                    {fmtPct(e.change_pct)}
                  </td>
                  <td className="px-4 py-3 text-right font-mono text-muted-foreground">
                    {fmtPrice(Number(e.price) || 0)}
                  </td>
                  <td className="px-4 py-3 text-right font-mono text-muted-foreground">
                    {fmtPrice(Number(e.high_24h) || 0)}
                  </td>
                  <td className="px-4 py-3 text-right font-mono text-muted-foreground">
                    {formatVolume({
                      value: e.volume_24h_usd,
                      partial: false,
                    })}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  );
}

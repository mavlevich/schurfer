import { useCallback, useEffect, useMemo, useState } from 'react';
import { Play, Square } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Nav } from '@/components/Nav';

interface Balance {
  exchange: string;
  wallet: string;
  asset: string;
  tradeable: boolean;
  free: number;
  used: number;
  total: number;
  usd_value: number;
}

interface WalletRow {
  exchange: string;
  wallet: string;
  tradeable: boolean;
  usd_total: number;
}

interface BalanceData {
  balances: Balance[];
  total_usd: number;
  total_usd_all: number;
  failed_exchanges: string[];
}

interface Position {
  exchange: string;
  symbol: string;
  base: string;
  side: string;
  size_usd: number;
  entry_price: number;
  unrealized_pnl: number;
  leverage: number;
  liquidation_price: number | null;
}

interface PositionsData {
  positions: Position[];
  count: number;
}

interface RiskData {
  trading_enabled: boolean;
  open_positions: number;
  max_positions: number;
  slots_free: number;
  daily_pnl_usd: number;
  daily_loss_limit_usd: number;
}

function usd(n: number, digits = 2): string {
  return n.toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function AccountPage() {
  const [balance, setBalance] = useState<BalanceData | null>(null);
  const [positions, setPositions] = useState<PositionsData | null>(null);
  const [risk, setRisk] = useState<RiskData | null>(null);
  const [initialized, setInitialized] = useState(false);
  const [unavailable, setUnavailable] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [toggling, setToggling] = useState(false);
  const [toggleError, setToggleError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [balRes, posRes, riskRes] = await Promise.all([
        fetch('/api/account/balance'),
        fetch('/api/account/positions'),
        fetch('/api/account/risk'),
      ]);
      if (!balRes.ok || !posRes.ok || !riskRes.ok) {
        setUnavailable(true);
        setInitialized(true);
        return;
      }
      setBalance((await balRes.json()) as BalanceData);
      setPositions((await posRes.json()) as PositionsData);
      setRisk((await riskRes.json()) as RiskData);
      setUnavailable(false);
      setLastUpdated(new Date());
    } catch {
      setUnavailable(true);
    } finally {
      setInitialized(true);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const id = setInterval(() => void refresh(), 15_000);
    return () => clearInterval(id);
  }, [refresh]);

  async function toggleTrading() {
    if (!risk || toggling) return;
    setToggling(true);
    setToggleError(null);
    try {
      const url = risk.trading_enabled ? '/api/account/stop' : '/api/account/resume';
      const res = await fetch(url, { method: 'POST' });
      if (!res.ok) {
        setToggleError(`Request failed (${res.status})`);
        return;
      }
      await refresh();
    } catch {
      setToggleError('Request failed');
    } finally {
      setToggling(false);
    }
  }

  const walletRows = useMemo<WalletRow[]>(() => {
    if (!balance) return [];
    const map = new Map<string, WalletRow>();
    for (const b of balance.balances) {
      const key = `${b.exchange}-${b.wallet}`;
      const existing = map.get(key);
      if (existing) {
        existing.usd_total += b.usd_value;
      } else {
        map.set(key, {
          exchange: b.exchange,
          wallet: b.wallet,
          tradeable: b.tradeable,
          usd_total: b.usd_value,
        });
      }
    }
    return Array.from(map.values());
  }, [balance]);

  const lossRatio = risk
    ? Math.min(1, Math.abs(Math.min(0, risk.daily_pnl_usd)) / risk.daily_loss_limit_usd)
    : 0;

  return (
    <div className="min-h-screen bg-background">
      <Nav />
      <div className="mx-auto max-w-2xl space-y-6 p-4 md:p-8">
        <div className="flex items-center justify-between">
          <h1 className="text-xl font-bold tracking-tight">Account</h1>
          <div className="flex items-center gap-3">
            {risk && (
              <Badge variant={risk.trading_enabled ? 'success' : 'destructive'}>
                {risk.trading_enabled ? 'Trading active' : 'Trading stopped'}
              </Badge>
            )}
            {lastUpdated && (
              <span className={`text-xs ${unavailable ? 'text-warning' : 'text-muted-foreground'}`}>
                {lastUpdated.toLocaleTimeString()}
                {unavailable && ' (stale)'}
              </span>
            )}
          </div>
        </div>

        {initialized && unavailable && !balance && (
          <Card>
            <CardContent className="py-4">
              <p className="text-sm text-muted-foreground">Connecting to execution service...</p>
            </CardContent>
          </Card>
        )}

        {/* Risk */}
        <Card>
          <CardHeader>
            <CardTitle className="text-sm font-medium uppercase tracking-wider text-muted-foreground">
              Risk
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4 pt-0">
            <div className="flex items-center justify-between">
              <span className="text-sm">Daily P&L</span>
              <span className="font-mono text-sm">
                {risk ? (
                  <>
                    <span
                      className={risk.daily_pnl_usd < 0 ? 'text-destructive' : 'text-green-500'}
                    >
                      {risk.daily_pnl_usd >= 0 ? '+' : '-'}${usd(Math.abs(risk.daily_pnl_usd))}
                    </span>
                    <span className="text-muted-foreground">
                      {' '}
                      / -${usd(risk.daily_loss_limit_usd)} limit
                    </span>
                  </>
                ) : (
                  '—'
                )}
              </span>
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-muted">
              <div
                className={`h-full rounded-full transition-all ${lossRatio >= 0.8 ? 'bg-destructive' : 'bg-amber-500'}`}
                style={{ width: `${lossRatio * 100}%` }}
              />
            </div>
            <div className="flex items-center justify-between">
              <span className="text-sm">Positions</span>
              <span className="font-mono text-sm text-muted-foreground">
                {risk ? `${risk.open_positions} / ${risk.max_positions} slots` : '—'}
              </span>
            </div>
            <div className="flex items-center justify-end gap-3 pt-1">
              {toggleError && <span className="text-xs text-destructive">{toggleError}</span>}
              <Button
                variant={risk?.trading_enabled ? 'destructive' : 'default'}
                size="sm"
                onClick={() => void toggleTrading()}
                disabled={toggling || !risk || unavailable}
                className="gap-1.5"
              >
                {risk?.trading_enabled ? (
                  <>
                    <Square className="h-3.5 w-3.5" />
                    Emergency stop
                  </>
                ) : (
                  <>
                    <Play className="h-3.5 w-3.5" />
                    Resume
                  </>
                )}
              </Button>
            </div>
          </CardContent>
        </Card>

        {/* Balance */}
        <Card>
          <CardHeader>
            <CardTitle className="text-sm font-medium uppercase tracking-wider text-muted-foreground">
              Balance
            </CardTitle>
          </CardHeader>
          <CardContent className="pt-0">
            {!initialized || !balance ? (
              <p className="text-sm text-muted-foreground">—</p>
            ) : balance.balances.length === 0 ? (
              <p className="text-sm text-muted-foreground">No exchanges configured</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[280px] text-sm">
                  <thead>
                    <tr className="text-xs text-muted-foreground">
                      <th className="pb-2 text-left font-normal">Exchange</th>
                      <th className="pb-2 text-left font-normal">Wallet</th>
                      <th className="pb-2 text-right font-normal">Total (USD)</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {walletRows.map((w) => {
                      const failed = balance.failed_exchanges.includes(w.exchange);
                      return (
                        <tr
                          key={`${w.exchange}-${w.wallet}`}
                          className={failed ? 'opacity-50' : ''}
                        >
                          <td className="py-2 capitalize">
                            {w.exchange}
                            {failed && <span className="ml-1 text-xs text-warning">⚠</span>}
                          </td>
                          <td className="py-2">
                            <span
                              className={`text-xs ${w.tradeable ? 'text-foreground' : 'text-muted-foreground'}`}
                            >
                              {w.wallet === 'spot'
                                ? 'Spot'
                                : w.wallet === 'fund'
                                  ? 'Funding'
                                  : 'Futures'}
                            </span>
                          </td>
                          <td
                            className={`py-2 text-right font-mono ${failed ? 'text-muted-foreground' : ''}`}
                          >
                            {failed ? '—' : `$${usd(w.usd_total)}`}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                  <tfoot>
                    <tr className="border-t border-border text-muted-foreground">
                      <td className="pt-3 text-xs" colSpan={2}>
                        All assets
                      </td>
                      <td className="pt-3 text-right font-mono text-xs">
                        ${usd(balance.total_usd_all)}
                      </td>
                    </tr>
                    <tr className="font-medium">
                      <td className="pt-1 text-xs" colSpan={2}>
                        Tradeable USDT
                      </td>
                      <td className="pt-1 text-right font-mono">${usd(balance.total_usd)}</td>
                    </tr>
                  </tfoot>
                </table>
              </div>
            )}
          </CardContent>
        </Card>

        {/* Open Positions */}
        <Card>
          <CardHeader>
            <CardTitle className="text-sm font-medium uppercase tracking-wider text-muted-foreground">
              Open Positions
            </CardTitle>
          </CardHeader>
          <CardContent className="pt-0">
            {!initialized || !positions ? (
              <p className="text-sm text-muted-foreground">—</p>
            ) : positions.count === 0 ? (
              <p className="text-sm text-muted-foreground">No open positions</p>
            ) : (
              <div className="space-y-2">
                {positions.positions.map((p, i) => (
                  <div
                    key={i}
                    className="flex items-start justify-between rounded-lg border border-border p-3"
                  >
                    <div className="space-y-1">
                      <div className="flex items-center gap-2">
                        <span className="font-medium">{p.base}</span>
                        <Badge
                          variant={p.side === 'short' ? 'destructive' : 'success'}
                          className="text-xs"
                        >
                          {p.side.toUpperCase()}
                        </Badge>
                        <span className="text-xs text-muted-foreground">{p.exchange}</span>
                      </div>
                      <div className="text-xs text-muted-foreground">
                        Entry ${usd(p.entry_price, 4)} · {p.leverage}x
                        {p.liquidation_price != null && ` · Liq $${usd(p.liquidation_price, 4)}`}
                      </div>
                    </div>
                    <div className="text-right">
                      <div className="font-mono text-sm">${usd(p.size_usd)}</div>
                      <div
                        className={`font-mono text-xs ${p.unrealized_pnl >= 0 ? 'text-green-500' : 'text-destructive'}`}
                      >
                        {p.unrealized_pnl >= 0 ? '+' : ''}
                        {usd(p.unrealized_pnl)}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

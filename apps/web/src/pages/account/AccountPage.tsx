import { useMemo, useState } from 'react';
import { Play, Square } from 'lucide-react';
import { useQueryClient } from '@tanstack/react-query';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { PageShell } from '@/components/shared/PageShell';
import { useBalance, usePositions, useRisk } from '@/hooks/useAccountData';
import type { Balance } from '@/hooks/useAccountData';

interface WalletRow {
  exchange: string;
  wallet: string;
  tradeable: boolean;
  usd_total: number;
}

function usd(n: number, digits = 2): string {
  return n.toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function walletLabel(wallet: string) {
  if (wallet === 'spot') return 'Spot';
  if (wallet === 'fund') return 'Funding';
  return 'Futures';
}

function aggregateWallets(balances: Balance[]): WalletRow[] {
  const map = new Map<string, WalletRow>();
  for (const b of balances) {
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
}

export function AccountPage() {
  const queryClient = useQueryClient();
  const [toggling, setToggling] = useState(false);
  const [toggleError, setToggleError] = useState<string | null>(null);

  const { data: balance, isError: balanceError, dataUpdatedAt: balanceUpdatedAt } = useBalance();
  const { data: positions, isError: positionsError } = usePositions();
  const { data: risk, isError: riskError } = useRisk();

  const unavailable = balanceError || positionsError || riskError;
  const initialized = balance !== undefined || balanceError;
  const lastUpdated = balanceUpdatedAt ? new Date(balanceUpdatedAt) : null;

  const walletRows = useMemo<WalletRow[]>(
    () => (balance ? aggregateWallets(balance.balances) : []),
    [balance],
  );

  const lossRatio = risk
    ? Math.min(1, Math.abs(Math.min(0, risk.daily_pnl_usd)) / risk.daily_loss_limit_usd)
    : 0;

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
      await queryClient.invalidateQueries({ queryKey: ['account'] });
    } catch {
      setToggleError('Request failed');
    } finally {
      setToggling(false);
    }
  }

  return (
    <PageShell width="narrow">
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
              {lastUpdated.toLocaleTimeString('en-US')}
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
                  <span className={risk.daily_pnl_usd < 0 ? 'text-destructive' : 'text-green-500'}>
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
                    const failed = (balance.failed_exchanges ?? []).includes(w.exchange);
                    return (
                      <tr key={`${w.exchange}-${w.wallet}`} className={failed ? 'opacity-50' : ''}>
                        <td className="py-2 capitalize">
                          {w.exchange}
                          {failed && <span className="ml-1 text-xs text-warning">⚠</span>}
                        </td>
                        <td className="py-2">
                          <span
                            className={`text-xs ${w.tradeable ? 'text-foreground' : 'text-muted-foreground'}`}
                          >
                            {walletLabel(w.wallet)}
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
    </PageShell>
  );
}

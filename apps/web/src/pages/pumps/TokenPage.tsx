import { Link, useParams } from 'react-router';
import { ArrowLeft } from 'lucide-react';
import { PageShell } from '@/components/shared/PageShell';
import { useToken } from '@/hooks/useTokenData';
import { fmtPct, pctColor } from '@/lib/formatters';

import { TokenChart } from './components/token/TokenChart';
import { TokenSignals } from './components/token/TokenSignals';
import { ExchangeBreakdown } from './components/token/ExchangeBreakdown';
import { TokenEpisodes } from './components/token/TokenEpisodes';
import { TokenStats } from './components/token/TokenStats';

export function TokenPage() {
  const { base } = useParams<{ base: string }>();

  // We still fetch useToken here to show the main H1 title and basic token info.
  // The sub-components will fetch their own data (which will be instantly satisfied from React Query cache)
  const { data: pump, isPending, isError } = useToken(base);

  const notFound = !isPending && !isError && !pump;

  return (
    <PageShell width="wide" className="space-y-4">
      <Link
        to="/pumps"
        className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground transition-colors"
      >
        <ArrowLeft className="h-3 w-3" />
        Scanner
      </Link>

      {isPending && <p className="text-sm text-muted-foreground">Loading token metadata...</p>}
      {notFound && <p className="text-sm text-muted-foreground">Token not found.</p>}
      {!isPending && isError && (
        <p className="text-sm text-red-400">Unable to load token details. Please retry.</p>
      )}

      {pump && (
        <div>
          <h1 className="text-2xl font-bold font-mono tracking-tight">
            {base}
            <span className={`ml-3 text-xl ${pctColor(pump.max_change_pct)}`}>
              {fmtPct(pump.max_change_pct)}
            </span>
          </h1>
          <p className="text-sm text-muted-foreground mt-1">
            Active on {pump.exchanges.length} exchange{pump.exchanges.length !== 1 ? 's' : ''}
          </p>
        </div>
      )}

      {base && (
        <>
          <TokenChart base={base} />
          <TokenSignals base={base} />
          <ExchangeBreakdown base={base} />
          <TokenEpisodes base={base} />
          <TokenStats base={base} />
        </>
      )}
    </PageShell>
  );
}

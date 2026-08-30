import { Link, useParams } from 'react-router';
import { ArrowLeft } from 'lucide-react';
import { PageShell } from '@/components/shared/PageShell';
import { useToken } from '@/hooks/useTokenData';
import { Percent } from '@/components/ui/domain/Percent';
import { isPumpEntry } from '@/pages/pumps/types';

import { TokenChart } from './components/token/TokenChart';
import { TokenSignals } from './components/token/TokenSignals';
import { ExchangeBreakdown } from './components/token/ExchangeBreakdown';
import { TokenEpisodes } from './components/token/TokenEpisodes';
import { TokenStats } from './components/token/TokenStats';

export function TokenPage() {
  const { base } = useParams<{ base: string }>();

  // We still fetch useToken here to show the main H1 title and basic token info.
  // The sub-components will fetch their own data (which will be instantly satisfied from React Query cache)
  const { data, isPending, isError } = useToken(base);

  // fix/token-activity-non-pump-assets-v1: a base can come back with no pump
  // episode but real activity in another strategy -- that is not "not
  // found" and gets its own message below, distinct from a genuinely
  // unknown base.
  const pump = data && isPumpEntry(data) ? data : undefined;
  const noPumpEpisode = data && !isPumpEntry(data) ? data : undefined;
  const notFound = !isPending && !isError && !data;

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
      {noPumpEpisode && (
        <p className="text-sm text-muted-foreground">
          No pump episode recorded, but available through {noPumpEpisode.other_strategy_key}.
        </p>
      )}
      {!isPending && isError && (
        <p className="text-sm text-red-400">Unable to load token details. Please retry.</p>
      )}

      {pump && (
        <div>
          <h1 className="text-2xl font-bold font-mono tracking-tight">
            {base}
            <span className="ml-3 text-xl">
              <Percent value={pump.max_change_pct} />
            </span>
            {!pump.is_live && (
              <span className="ml-2 align-middle text-xs font-sans font-normal text-muted-foreground">
                (peak, not live)
              </span>
            )}
          </h1>
          <p className="text-sm text-muted-foreground mt-1">
            {pump.is_live ? 'Active on' : 'Last seen on'} {pump.exchanges.length} exchange
            {pump.exchanges.length !== 1 ? 's' : ''}
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

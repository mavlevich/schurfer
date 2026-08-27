import { useSearchParams } from 'react-router';
import { PageShell } from '@/components/shared/PageShell';
import { PumpTable } from './components/PumpTable';
import { MomentumWatchTable } from './components/MomentumWatchTable';

type ScannerTab = 'pump' | 'momentum_watch';

// ScannerTabButton mirrors the active/inactive treatment used by Nav's own
// NavLink, so switching surfaces within the Scanner page feels consistent
// with switching between pages.
function ScannerTabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded px-3 py-1.5 text-sm transition-colors ${
        active ? 'bg-accent text-accent-foreground' : 'text-muted-foreground hover:text-foreground'
      }`}
    >
      {children}
    </button>
  );
}

export function PumpsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = (searchParams.get('tab') as ScannerTab) || 'pump';

  const setTab = (newTab: ScannerTab) => {
    setSearchParams({ tab: newTab });
  };

  return (
    <PageShell width="wide" className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold tracking-tight">Scanner</h1>
        </div>
      </div>

      <div className="flex items-center gap-1 border-b pb-2">
        <ScannerTabButton active={tab === 'pump'} onClick={() => setTab('pump')}>
          Pump Scanner
        </ScannerTabButton>
        <ScannerTabButton
          active={tab === 'momentum_watch'}
          onClick={() => setTab('momentum_watch')}
        >
          Momentum Flow (long)
        </ScannerTabButton>
      </div>

      {tab === 'momentum_watch' && <MomentumWatchTable />}
      {tab === 'pump' && <PumpTable />}
    </PageShell>
  );
}

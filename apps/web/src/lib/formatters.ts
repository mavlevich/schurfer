export function fmtPct(n: number) {
  return `${n >= 0 ? '+' : ''}${n.toFixed(1)}%`;
}

export function pctColor(pct: number) {
  if (pct >= 100) return 'text-red-400';
  if (pct >= 50) return 'text-orange-400';
  if (pct > 0) return 'text-yellow-400';
  return 'text-muted-foreground';
}

export function timeAgo(sec: number) {
  const diff = Math.floor(Date.now() / 1000 - sec);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  return `${Math.floor(diff / 3600)}h ago`;
}

export function fmtPrice(n: number): string {
  if (n === 0) return '—';
  if (n >= 1000) return `$${n.toLocaleString('en-US', { maximumFractionDigits: 2 })}`;
  if (n >= 1) return `$${n.toFixed(4)}`;
  if (n >= 0.0001) return `$${n.toFixed(6)}`;
  return `$${n.toPrecision(4)}`;
}

export function fmtSignedPct(n: number | null): string {
  if (n === null) return '—';
  return `${n >= 0 ? '+' : ''}${n.toFixed(2)}%`;
}

export function fmtUsdCompact(n: number | null): string {
  if (n === null) return '—';
  const sign = n < 0 ? '-' : '';
  const abs = Math.abs(n);
  if (abs >= 1_000_000) return `${sign}$${(abs / 1_000_000).toFixed(2)}M`;
  if (abs >= 1_000) return `${sign}$${(abs / 1_000).toFixed(1)}k`;
  return `${sign}$${abs.toFixed(0)}`;
}

export function signedColor(n: number | null): string {
  if (n === null) return 'text-muted-foreground';
  return n >= 0 ? 'text-green-400' : 'text-red-400';
}

export function formatDate(value: string): string {
  return new Date(value).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    timeZone: 'UTC',
  });
}

export function formatDateTime(value: string): string {
  return new Date(value).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    timeZone: 'UTC',
  });
}

export function formatBytes(value: number): string {
  if (value === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(value) / Math.log(k));
  return `${Number.parseFloat((value / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`;
}

export function formatMetric(value: number | null, suffix: string, digits = 1): string {
  if (value === null || value === undefined) return '—';
  return `${value.toFixed(digits)}${suffix}`;
}

export function formatLatency(value: number | null): string {
  if (value === null || value === undefined) return '—';
  return `${value.toFixed(0)}ms`;
}

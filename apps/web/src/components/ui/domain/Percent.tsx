import { cn } from '@/lib/utils';
import { fmtPct, pctColor, signedColor } from '@/lib/formatters';

interface PercentProps extends React.HTMLAttributes<HTMLSpanElement> {
  value: number | null | undefined;
  colorize?: boolean; // Deprecated, use theme="none"
  theme?: 'pump' | 'signed' | 'none';
  tabular?: boolean;
  fallback?: string;
}

export function Percent({
  value,
  colorize = true,
  theme = 'pump',
  tabular = true,
  fallback = '—',
  className,
  ...props
}: PercentProps) {
  if (value === null || value === undefined) {
    return (
      <span
        className={cn('text-muted-foreground', tabular && 'tabular-nums', className)}
        {...props}
      >
        {fallback}
      </span>
    );
  }

  // Backwards compatibility for colorize=false
  const activeTheme = colorize === false ? 'none' : theme;

  let colorClass = '';
  if (activeTheme === 'pump') colorClass = pctColor(value);
  else if (activeTheme === 'signed') colorClass = signedColor(value);

  return (
    <span className={cn(tabular && 'tabular-nums font-mono', colorClass, className)} {...props}>
      {fmtPct(value)}
    </span>
  );
}

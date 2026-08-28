import { cn } from '@/lib/utils';
import { timeAgo } from '@/lib/formatters';

interface TimeFormattedProps extends React.HTMLAttributes<HTMLSpanElement> {
  value: number | string | null | undefined; // unix seconds or iso string
  format?: 'relative' | 'absolute';
  tabular?: boolean;
  fallback?: string;
}

export function TimeFormatted({
  value,
  format = 'absolute',
  tabular = true,
  fallback = '—',
  className,
  ...props
}: TimeFormattedProps) {
  if (!value) {
    return (
      <span
        className={cn('text-muted-foreground', tabular && 'tabular-nums', className)}
        {...props}
      >
        {fallback}
      </span>
    );
  }

  const isUnixSeconds = typeof value === 'number' && value < 10000000000;
  const dateObj =
    typeof value === 'string' ? new Date(value) : new Date(isUnixSeconds ? value * 1000 : value);

  let formatted = '';

  if (format === 'relative') {
    const unixSeconds = isUnixSeconds ? (value as number) : Math.floor(dateObj.getTime() / 1000);
    formatted = timeAgo(unixSeconds);
  } else {
    formatted = dateObj.toLocaleString('en-US', {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  }

  return (
    <span
      className={cn('text-muted-foreground', tabular && 'tabular-nums font-mono', className)}
      {...props}
    >
      {formatted}
    </span>
  );
}

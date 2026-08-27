import { cn } from '@/lib/utils';
import { fmtPrice } from '@/lib/formatters';

interface PriceProps extends React.HTMLAttributes<HTMLSpanElement> {
  value: number | string | null | undefined;
  tabular?: boolean;
  fallback?: string;
}

export function Price({ value, tabular = true, fallback = '—', className, ...props }: PriceProps) {
  if (value === null || value === undefined || value === '') {
    return (
      <span
        className={cn('text-muted-foreground', tabular && 'tabular-nums', className)}
        {...props}
      >
        {fallback}
      </span>
    );
  }

  const numValue = typeof value === 'string' ? parseFloat(value) : value;

  if (isNaN(numValue)) {
    return (
      <span
        className={cn('text-muted-foreground', tabular && 'tabular-nums', className)}
        {...props}
      >
        {fallback}
      </span>
    );
  }

  return (
    <span
      className={cn(
        'text-muted-foreground', // Default price color
        tabular && 'tabular-nums font-mono',
        className,
      )}
      {...props}
    >
      {fmtPrice(numValue)}
    </span>
  );
}

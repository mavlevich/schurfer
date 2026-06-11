import { cn } from '@/lib/utils';

type Status = 'up' | 'down' | 'unknown';

interface StatusDotProps {
  status: Status;
  className?: string;
}

const colors: Record<Status, string> = {
  up: 'bg-success',
  down: 'bg-destructive',
  unknown: 'bg-muted-foreground',
};

const pulseColors: Record<Status, string> = {
  up: 'bg-success',
  down: 'bg-destructive',
  unknown: 'bg-muted-foreground',
};

export function StatusDot({ status, className }: StatusDotProps) {
  return (
    <span className={cn('relative flex h-2.5 w-2.5', className)}>
      {status === 'up' && (
        <span
          className={cn(
            'absolute inline-flex h-full w-full animate-ping rounded-full opacity-75',
            pulseColors[status],
          )}
        />
      )}
      <span className={cn('relative inline-flex h-2.5 w-2.5 rounded-full', colors[status])} />
    </span>
  );
}

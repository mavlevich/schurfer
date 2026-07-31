import type { ReactNode } from 'react';
import { Nav } from '@/components/Nav';
import { cn } from '@/lib/utils';

type PageWidth = 'narrow' | 'content' | 'wide';

const widths: Record<PageWidth, string> = {
  narrow: 'max-w-2xl',
  content: 'max-w-4xl',
  wide: 'max-w-6xl',
};

interface PageShellProps {
  children: ReactNode;
  width?: PageWidth;
  className?: string;
}

export function PageShell({ children, width = 'wide', className }: PageShellProps) {
  return (
    <div className="min-h-screen bg-background">
      <Nav />
      <main className={cn('mx-auto space-y-6 p-4 md:p-8', widths[width], className)}>
        {children}
      </main>
    </div>
  );
}

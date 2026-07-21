import { keepPreviousData, useQuery } from '@tanstack/react-query';

export interface Decision {
  id: number;
  ts: string;
  base: string;
  exchange: string;
  action: string;
  reason: string;
  score: number | null;
  pump_pct: number | null;
  price: number | null;
}

interface DecisionsResponse {
  total: number;
  limit: number;
  offset: number;
  decisions: Decision[];
}

interface UseDecisionsParams {
  base?: string;
  action?: string;
  limit: number;
  offset: number;
}

export function useDecisions({ base, action, limit, offset }: UseDecisionsParams) {
  const params = new URLSearchParams();
  if (base) params.set('base', base);
  if (action) params.set('action', action);
  params.set('limit', String(limit));
  params.set('offset', String(offset));

  return useQuery<DecisionsResponse>({
    queryKey: ['decisions', base, action, limit, offset],
    queryFn: async () => {
      const res = await fetch(`/api/decisions?${params}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json() as Promise<DecisionsResponse>;
    },
    staleTime: 30_000,
    refetchInterval: 60_000,
    placeholderData: keepPreviousData,
  });
}

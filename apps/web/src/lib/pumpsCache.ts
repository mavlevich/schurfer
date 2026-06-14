import type { PumpsResponse, HistoryEntry } from '@/pages/pumps/types';

interface CacheEntry<T> {
  data: T;
  ts: number;
}

function makeCache<T>(ttlMs: number) {
  let entry: CacheEntry<T> | null = null;
  return {
    get(): T | null {
      if (entry && Date.now() - entry.ts < ttlMs) return entry.data;
      return null;
    },
    set(data: T) {
      entry = { data, ts: Date.now() };
    },
  };
}

export interface PumpsSnapshot {
  live: PumpsResponse;
  history: HistoryEntry[];
}

export const pumpsCache = makeCache<PumpsSnapshot>(60_000);

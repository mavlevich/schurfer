import type { ExchangeEntry } from './types';

export interface VolumeSummary {
  value: number | null;
  partial: boolean;
}

function isKnownVolume(value: number | null): value is number {
  return value !== null && Number.isFinite(value) && value > 0;
}

export function summarizeVolume(exchanges: ExchangeEntry[]): VolumeSummary {
  let total = 0;
  let known = 0;
  let partial = false;

  for (const exchange of exchanges) {
    if (!isKnownVolume(exchange.volume_24h_usd)) {
      partial = true;
      continue;
    }
    total += exchange.volume_24h_usd;
    known++;
  }

  return {
    value: known > 0 ? total : null,
    partial,
  };
}

export function formatVolume({ value, partial }: VolumeSummary): string {
  if (!isKnownVolume(value)) return 'n/a';

  let formatted: string;
  if (value >= 1e9) formatted = `$${(value / 1e9).toFixed(1)}B`;
  else if (value >= 1e6) formatted = `$${(value / 1e6).toFixed(0)}M`;
  else formatted = `$${value.toFixed(0)}`;

  return partial ? `${formatted}+` : formatted;
}

export function volumeRank(value: number | null): number {
  return isKnownVolume(value) ? value : 0;
}

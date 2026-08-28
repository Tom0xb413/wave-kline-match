export type TF = "1H" | "4H" | "12H" | "1D";

export interface Bar {
  ts: number;
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  is_closed: boolean;
}

export interface AssetLast {
  ts: number;
  close: number;
  change: number;
  time: string;
}

export interface UniverseAsset {
  id: string;
  class: string;
  venue: string;
  native_symbol: string;
  ready: boolean;
  last: AssetLast | null;
  adhoc?: boolean;
}

export interface SearchHit {
  id: string;
  venue: string;
  native_symbol: string;
  class: string;
  ready: boolean;
  source: string;
}

export interface Hit {
  rank: number;
  asset: string;
  tf: string;
  start_ts: number;
  end_ts: number;
  start_utc: string;
  end_utc: string;
  bars: number;
  pearson_r: number;
  score: number;
  venue: string;
  zscore: number[];
  kind: string;
  forward_ret?: number[];
  native_symbol?: string;
  r_close?: number | null;
  r_shape?: number | null;
  r_volume?: number | null;
  weights?: { close: number; shape: number; volume: number } | null;
}

export interface ChannelWeights {
  close: number;
  shape: number;
  volume: number;
}

export interface WeightPreset {
  id: string;
  name_zh: string;
  w_close: number | null;
  w_shape: number | null;
  w_volume: number | null;
}

export interface ForwardStep {
  i: number;
  n: number;
  p25: number;
  p50: number;
  p75: number;
  pct_up: number;
}

export interface ForwardDist {
  horizon: number;
  n_hits: number;
  n_with_full: number;
  steps: ForwardStep[];
}

export interface MatchQuery {
  asset: string;
  tf: string;
  n: number;
  venue: string;
  start_ts: number;
  end_ts: number;
  start_utc: string;
  end_utc: string;
  native_symbol: string;
  drawn?: boolean;
  pattern?: string;
  live_scan?: {
    live?: boolean;
    n_symbols?: number;
    n_scored?: number;
    n_failed?: number;
    error?: string | null;
  };
  sector?: string;
  history_pool?: string[];
  resonance_pool?: string[];
  weights?: ChannelWeights;
}

export interface WavePattern {
  id: string;
  name_zh: string;
  suggested_n: number;
  path?: number[];
}

export interface WaveTemplate {
  id: string;
  name: string;
  tf: TF;
  n: number;
  path: number[];
  created_at: string;
}

export interface MatchResponse {
  query: MatchQuery;
  query_z: number[];
  resonance: Hit[];
  history: Hit[];
  forward: ForwardDist | null;
}

export interface BootState {
  state: string;
  message: string;
  started_at: string | null;
  finished_at: string | null;
  errors: number;
}

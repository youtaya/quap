/**
 * 后端响应的类型。字段名与 `src/quant_platform/api/` 里返回的键**逐字对应**——没有 `/openapi.json`
 * （`docs_url`/`openapi_url` 在 `create_app` 里被有意关掉），所以这份文件是唯一的前端契约记录。
 * 改后端响应时，这里必须同步；反之亦然。
 */

/* ── 判决面：GET /data-readiness ─────────────────────────────────────────── */

export type GateStatus = 'passed' | 'blocked' | 'waiting';
export type GateAction = 'auto' | 'manual' | 'ops';
export type GateStage = 'foundation' | 'data-model' | 'output';

/** 门禁的判定证据。键随门禁而异，是判决句里那些数字的唯一来源。 */
export interface GateEvidence {
  /* capability */
  missing?: string[];
  missing_labels?: string[];
  required?: string[];
  verified?: string[];
  required_count?: number;
  missing_count?: number;
  /* scope */
  scope?: string;
  required_scope?: string;
  minimum_coverage?: number;
  /* engine */
  required_roles?: string[];
  healthy_roles?: string[];
  healthy?: boolean;
  /* generation / model */
  frequency?: string;
  generation_count?: number;
  /* day_baseline */
  missing_frequency?: string;
  /* recommendation —— 前端由 `output` 合成的第三阶段 */
  published?: boolean;
  valid_until?: string;
  recommendation_id?: string;
}

export interface Gate {
  key: string;
  stage: GateStage;
  stage_label: string;
  action: GateAction;
  depends_on: string[];
  status: GateStatus;
  evidence: GateEvidence;
}

/** ⑥ 产出状态。**不参与** `ready`：`ready` 问「能不能跑」，这里问「跑出来的东西在不在」。 */
export interface OutputState {
  published: boolean;
  depends_on: string[];
  recommendation_id: string | null;
  as_of: string | null;
  valid_until: string | null;
}

export interface FrequencyReadiness {
  ready: boolean;
  /** 机器可读原因，**不直接展示**——判决句由 `domain/verdict.ts` 按 `gates[].key` 组稿。 */
  blockers: string[];
  model: ModelRecord | null;
  gates: Gate[];
  output: OutputState;
}

export interface Capability {
  endpoint: string;
  status: string;
  updated_at?: string;
  data: Record<string, unknown>;
}

export interface Readiness {
  engine: string;
  required: boolean;
  native_fallback: boolean;
  /** 后端恒定返回这两条频率（`pipeline.readiness` 的循环固定遍历 day/5min），所以类型写死而不是
   *  `Record<string, …>`——后者在 `noUncheckedIndexedAccess` 下会逼出满屏的无意义空值判断。 */
  frequencies: { day: FrequencyReadiness; '5min': FrequencyReadiness };
  capabilities: Capability[];
  generations: Generation[];
  history_sessions: number;
  factor_sessions: number;
  minute_history_sessions: number;
  pool_capacity: number;
  research_minutes: Record<string, unknown>;
}

/* ── 运行与模型 ──────────────────────────────────────────────────────────── */

export interface Generation {
  id: string;
  frequency: string;
  created_at: string;
  manifest: Record<string, unknown> | null;
  origin?: string;
}

export interface PipelineRun {
  id: string;
  frequency: string;
  purpose: string;
  as_of: string;
  state: string;
  error: string | null;
  result: Record<string, unknown> | null;
  created_at: string;
}

export interface ModelRecord {
  id: string;
  frequency: string;
  state: string;
  created_at: string;
  expires_at?: string;
  trained_as_of?: string;
  metadata?: Record<string, unknown>;
  evaluation?: Record<string, unknown>;
  passed?: boolean;
}

export interface ShadowSample {
  id: number;
  model_id: string;
  day: string;
  data: Record<string, unknown>;
}

/* ── 组合与建议 ──────────────────────────────────────────────────────────── */

export interface ModelPortfolio {
  id: string;
  name: string;
  revision: number;
  cash_weight: number;
  weights: Record<string, number>;
  created_at: string;
  effective_from?: string;
  data?: Record<string, unknown>;
}

export interface PortfolioRevision {
  id: string;
  portfolio_id: string;
  revision: number;
  effective_from: string;
  data: Record<string, unknown>;
  created_at: string;
}

export interface RecommendationRow {
  id: string;
  frequency: string;
  as_of: string;
  available_at: string;
  valid_until: string;
  state: string;
  policy_revision: number;
  portfolio_id: string | null;
  portfolio_revision: number | null;
  data: RecommendationData;
  model_id: string;
  generation_id: string;
  valid: boolean;
  acceptance_open: boolean;
  snapshot?: Record<string, unknown>;
}

export interface RecommendationData {
  stocks?: StockView[];
  cash_weight?: number;
  book?: Record<string, unknown>;
  summary?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface StockView {
  symbol: string;
  name?: string;
  score?: number;
  rank?: number;
  close?: number;
  change?: number;
  baseline_weight?: number;
  target_weight?: number;
  action?: string;
  [key: string]: unknown;
}

export interface StockState {
  symbol: string;
  state: 'available' | 'unavailable';
  reason?: string;
  model_view?: StockView;
  portfolio_id?: string | null;
  report_id?: string;
  as_of?: string;
  available_at?: string;
  valid_until?: string;
  orders: boolean;
}

export interface AcceptanceRecord {
  id: number;
  recommendation_id: string;
  name: string;
  created_at: string;
  data: Record<string, unknown>;
}

export interface RecommendationDetail extends RecommendationRow {
  human_decisions: AcceptanceRecord[];
}

/* ── 选股策略与观察清单 ──────────────────────────────────────────────────── */

export interface ScanPolicy {
  price_ceiling: number;
  minimum_sessions: number;
  minimum_turnover: number;
  top_n: number;
  max_weight: number;
  minimum_cash: number;
  change_band: number;
  minimum_coverage: number;
  exclude_risk_names: boolean;
}

export interface PolicyRecord {
  revision: number;
  data: ScanPolicy;
  created_at?: string;
}

export interface WatchlistRecord {
  revision: number;
  symbols: string[];
}

/* ── 运行总览：GET /status 与 GET /boards ────────────────────────────────── */

export interface Heartbeat {
  worker: string;
  role: string;
  updated_at: string;
  healthy: boolean;
  data: Record<string, unknown>;
}

export interface BoardStatus {
  board: string;
  listed: number;
  eligible?: number;
  [key: string]: unknown;
}

export interface OperationsStatus {
  provider: string;
  configured: boolean;
  boards: BoardStatus[];
  metrics: Record<string, unknown>;
  services: Heartbeat[];
  capabilities: Capability[];
  jobs: { queue: string; status: string; count: number }[];
  [key: string]: unknown;
}

export interface RuntimeSettings {
  revision: number;
  quote_seconds: number;
  provider: string;
  history_sessions: number;
  realtime_rpm: number;
  ordinary_rpm: number;
  daily_quota: number;
  qlib_enabled: boolean;
}

/* ── 研究报告：GET /qlib-report ──────────────────────────────────────────── */

export interface QlibReportSummary {
  samples: number | null;
  ic: number | null;
  rank_ic: number | null;
  daily_ic: number | null;
  quantiles: unknown;
  book: {
    status: string | null;
    rebalances: number | null;
    cumulative_return: number | null;
    benchmark_cumulative_return: number | null;
    relative_return: number | null;
    mean_turnover: number | null;
  };
}

export interface QlibReport {
  report: Record<string, unknown> | null;
  markdown: string | null;
  report_id?: number;
  as_of?: string;
  target?: string | null;
  engine?: string;
  summary?: QlibReportSummary;
  reason?: string;
}

/* ── 个股研究 ────────────────────────────────────────────────────────────── */

export interface Quote {
  symbol: string;
  data: { source_time?: string; close?: number; change?: number; [key: string]: unknown };
  fresh: boolean;
  [key: string]: unknown;
}

export interface Instrument {
  symbol: string;
  name: string;
  board: string;
  status: string;
  list_date: string | null;
}

export interface ScorePoint {
  id: number;
  model_id: string;
  generation_id: string;
  feature_cutoff: string;
  observation_time: string;
  score: number;
  rank: number;
}

/* ── 分页 ────────────────────────────────────────────────────────────────── */

export interface Page<T> {
  items: T[];
  limit: number;
  offset: number;
  next_offset: number | null;
}

/**
 * 格式化。三条硬约束：
 *
 * 1. **红涨绿跌**，不可反转（中国市场惯例）。涨用 `--quote-up`，跌用 `--quote-down`。
 *    这与欧美相反，所以颜色**不能**由「好/坏」推导，只能由「正/负」推导。
 * 2. 时刻一律转**北京时间**并标明，避免操作员把 UTC 当成盘中时间。
 * 3. 数字用 tabular-nums，否则表格里小数点在跳动。
 */

const CN = 'Asia/Shanghai';

const timeFormatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: CN,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
});

const dayFormatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: CN,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
});

/** 完整时刻，带时区标注。`2026-09-27 14:35 北京` */
export function formatMoment(value: unknown): string {
  const date = toDate(value);
  if (!date) return '—';
  return `${timeFormatter.format(date).replace(/\//g, '-')} 北京`;
}

/** 只到日。`2026-09-27` */
export function formatDay(value: unknown): string {
  const date = toDate(value);
  if (!date) return '—';
  return dayFormatter.format(date).replace(/\//g, '-');
}

/** 相对时长，用于「心跳」这类新鲜度判断。 */
export function formatAge(value: unknown, now: Date = new Date()): string {
  const date = toDate(value);
  if (!date) return '—';
  const seconds = Math.round((now.getTime() - date.getTime()) / 1000);
  if (seconds < 0) return '刚刚';
  if (seconds < 60) return `${seconds} 秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}

function toDate(value: unknown): Date | null {
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  if (typeof value !== 'string' && typeof value !== 'number') return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** 千分位整数。 */
export function formatInteger(value: unknown): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  return Math.round(value).toLocaleString('zh-CN');
}

/** 固定小数位。`digits` 默认 2。 */
export function formatNumber(value: unknown, digits = 2): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  return value.toFixed(digits);
}

/** 小数 → 百分比。输入是 0.1234，输出 `12.34%`。 */
export function formatPercent(value: unknown, digits = 2): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  return `${(value * 100).toFixed(digits)}%`;
}

/** 带符号的百分比，涨跌色用。输入是 0.0123，输出 `+1.23%`。 */
export function formatSignedPercent(value: unknown, digits = 2): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  const sign = value > 0 ? '+' : '';
  return `${sign}${(value * 100).toFixed(digits)}%`;
}

/**
 * 涨跌方向 → CSS 类名。**只依据符号**，不依据「这是不是好消息」——红涨绿跌是市场惯例，
 * 一个「下跌」在这里必须是绿的，哪怕它让持仓变差。
 */
export function changeClass(value: unknown): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value === 0) return 'qp-flat';
  return value > 0 ? 'qp-up' : 'qp-down';
}

/** 把秒数说成人话，用于「预计耗时」。 */
export function formatDuration(seconds: unknown): string {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds <= 0) return '—';
  if (seconds < 60) return `${Math.round(seconds)} 秒`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分钟`;
  return `${(seconds / 3600).toFixed(1)} 小时`;
}

/** 安全取值：`evidence` 与原始 JSON 都是 `unknown`，取字符串时统一走这里。 */
export function text(value: unknown, fallback = '—'): string {
  if (value === null || value === undefined || value === '') return fallback;
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return fallback;
}

/** 把原始 JSON 压成一行摘要，用于表格里的 `result` / `data` 列。 */
export function summarize(value: unknown, max = 120): string {
  if (value === null || value === undefined) return '—';
  if (typeof value !== 'object') return text(value);
  const entries = Object.entries(value as Record<string, unknown>);
  if (entries.length === 0) return '—';
  const rendered = entries
    .slice(0, 6)
    .map(([key, item]) => `${key}=${typeof item === 'object' && item !== null ? '{…}' : text(item)}`)
    .join(' ');
  return rendered.length > max ? `${rendered.slice(0, max)}…` : rendered;
}

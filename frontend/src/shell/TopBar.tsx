/**
 * 顶栏。**只放与判决无关的事实**（附录 B）。
 *
 * 反例是必须避免的：判决条写着「今天出不了建议」（红），顶栏却挂着绿色「系统正常」——操作员
 * 立刻会问「系统正常为什么出不了建议」。所以这里每一项都是**事实**（连不连得上、采了多少、
 * 行情多新），用中性色；唯一例外是「接口不可用」，那是事实层面的失败，用错误色。
 * 「今天能不能出建议」的唯一权威表述在判决条。
 */

import { useApi } from '@/api/useApi';
import type { OperationsStatus } from '@/api/types';
import { Badge } from '@/components/ui';
import { formatAge, formatPercent } from '@/domain/format';
import { BOARD_CODES } from '@/domain/labels';

function asNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

export function TopBar({
  title,
  description,
  onToggleNav,
}: {
  title: string;
  description: string;
  onToggleNav: () => void;
}) {
  const { data, error } = useApi<OperationsStatus>('/status');

  const coverage = asNumber(data?.metrics?.['eligible_market_coverage']);
  const latestQuote = asNumber(data?.metrics?.['source_age_seconds'] ? (data.metrics['source_age_seconds'] as Record<string, unknown>)['minimum'] : null);
  const paused = data?.metrics?.['polling_paused'] === true;
  const totalListed = (data?.boards ?? []).reduce((sum, board) => sum + (board.listed ?? 0), 0);

  return (
    <header className="qp-topbar">
      <button
        type="button"
        className="qp-btn qp-btn--quiet qp-navtoggle"
        onClick={onToggleNav}
        aria-label="打开工作空间导航"
      >
        <svg width="18" height="18" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
          <path d="M2.5 4h11M2.5 8h11M2.5 12h11" strokeLinecap="round" />
        </svg>
      </button>

      <div className="qp-topbar__title">{title}</div>
      <div className="qp-topbar__desc">{description}</div>

      <div className="qp-topbar__right">
        {error ? (
          <Badge tone="error" title={error.detail}>
            接口不可用
          </Badge>
        ) : data ? (
          <Badge tone="idle" title="前端与 API 之间的连通性。这与「今天能不能出建议」无关。">
            接口已连接
          </Badge>
        ) : (
          <Badge tone="idle">正在连接…</Badge>
        )}

        {data ? (
          <Badge tone="idle" plain className="qp-badge--secondary" title="行情源。免令牌公开源，无需授权令牌。">
            数据源 {data.provider?.toUpperCase() ?? '—'}
          </Badge>
        ) : null}

        {coverage !== null ? (
          <Badge
            tone={coverage >= 0.95 ? 'idle' : 'warn'}
            title={`有效样本覆盖 ${formatPercent(coverage)}；生产验收要求 ≥95%。上市合计 ${totalListed} 只。`}
          >
            覆盖 {formatPercent(coverage, 1)}
          </Badge>
        ) : null}

        {latestQuote !== null ? (
          <Badge tone="idle" plain className="qp-badge--secondary" title="最新行情的存储时间距今多久。">
            行情 {formatAge(new Date(Date.now() - latestQuote * 1000))}
          </Badge>
        ) : null}

        {paused ? (
          <Badge tone="warn" className="qp-badge--secondary" title="采集调度已暂停，不会拉取新数据。">
            采集已暂停
          </Badge>
        ) : null}
      </div>
    </header>
  );
}

/** 板块名 → 角标。导出给板块卡复用，保证角标只有一处定义。 */
export function boardCode(board: string): string {
  return BOARD_CODES[board] ?? board;
}

/**
 * 选股发现。用 Qlib 预测给低价股排序，**而不是按便宜程度**。
 *
 * 这个区别是这一屏最容易被误解的地方：操作员看到「低价股」会以为按价格从低到高排。所以页面上
 * 反复说明排序依据是模型预测，价格上限只是候选池的准入条件。
 */

import { useState } from 'react';

import { api } from '@/api/client';
import type { PolicyRecord, RecommendationRow, StockView } from '@/api/types';
import { useApi } from '@/api/useApi';
import { DataTable } from '@/components/DataTable';
import { Badge, Banner, Button, Card, EmptyState, Facts, LoadingBlock } from '@/components/ui';
import { useToast } from '@/components/toast';
import { changeClass, formatMoment, formatNumber, formatPercent, formatSignedPercent } from '@/domain/format';

export function Discovery() {
  const policy = useApi<PolicyRecord>('/scan-policy');
  const reports = useApi<RecommendationRow[]>('/recommendations?frequency=day&valid_only=false');
  const toast = useToast();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);

  const report = reports.data?.[0] ?? null;
  const candidates = report?.data.stocks ?? [];

  function toggle(code: string) {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(code)) next.delete(code);
      else next.add(code);
      return next;
    });
  }

  async function accept() {
    if (!report) return;
    setBusy(true);
    try {
      await api.post(`/recommendations/${report.id}/accept`, {
        portfolio_id: report.portfolio_id,
        selected_symbols: Array.from(selected),
        name: `选股发现 · ${formatMoment(report.as_of)}`,
        expected_revision: 0,
        expected_policy_revision: report.policy_revision,
        request_key: crypto.randomUUID(),
      });
      toast.push('ok', '已采纳所选调整', '生成了一个新的组合版本，供下一个交易日使用。');
      setSelected(new Set());
      reports.refresh();
    } catch (failure) {
      toast.push('error', '采纳失败', failure instanceof Error ? failure.message : '未知错误');
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="qp-heading">
        <h1>选股发现</h1>
        <p>用 Qlib 预测给低价股排序，而不是按便宜程度。价格上限只是候选池的准入条件。</p>
      </div>

      <Banner
        tone="info"
        title="排序依据是模型预测，不是价格"
        detail="候选池先按价格上限与流动性筛出，再按模型预测分数从高到低排。便宜不等于值得买，这一屏只提供模型的看法。"
      />

      <section className="qp-section">
        <Card title="候选池口径" meta={`策略版本 ${policy.data?.revision ?? '—'}`}>
          {policy.data ? (
            <Facts
              items={[
                { label: '价格上限', value: `¥${formatNumber(policy.data.data.price_ceiling)}` },
                { label: '最小上市交易日', value: formatNumber(policy.data.data.minimum_sessions, 0) },
                { label: '最小成交额', value: `¥${formatNumber(policy.data.data.minimum_turnover, 0)}` },
                { label: '取前 N 只', value: formatNumber(policy.data.data.top_n, 0) },
                { label: '单票上限', value: formatPercent(policy.data.data.max_weight, 1) },
                { label: '最小覆盖率', value: formatPercent(policy.data.data.minimum_coverage, 0) },
              ]}
            />
          ) : (
            <LoadingBlock />
          )}
        </Card>
      </section>

      <section className="qp-section">
        <h2 style={{ marginBottom: 'var(--space-3)' }}>模型排序的候选</h2>
        {reports.loading ? (
          <LoadingBlock />
        ) : reports.error ? (
          <Banner tone="error" title="读不到候选" detail={reports.error.detail} />
        ) : !report ? (
          <EmptyState>今天还没有候选报告。到「今日」查看是哪一环没通过。</EmptyState>
        ) : (
          <>
            {!report.valid ? (
              <Banner
                tone="warn"
                title="这份报告当前无效"
                detail="有效条件：模型已发布且未过期、策略版本为最新。到「今日」查看具体是哪一环。"
              />
            ) : (
              <Banner tone="ok" title="这份报告有效" detail={`可用至 ${formatMoment(report.valid_until)}。`} />
            )}

            <div className="qp-row" style={{ margin: 'var(--space-3) 0' }}>
              <Button variant="primary" disabled={busy || selected.size === 0 || !report.valid} onClick={accept}>
                {busy ? '正在提交…' : `采纳所选调整（${selected.size}）`}
              </Button>
              <Button variant="quiet" onClick={() => setSelected(new Set(candidates.map((row) => row.symbol)))}>
                全选
              </Button>
              <Button variant="quiet" onClick={() => setSelected(new Set())}>
                清空
              </Button>
              <span className="qp-caption">
                {report.valid ? '采纳不会下单，只会生成新的组合版本。' : '报告无效时不能采纳。'}
              </span>
            </div>

            <CandidatesTable
              candidates={candidates}
              selected={selected}
              onToggle={toggle}
            />
          </>
        )}
      </section>

      <section className="qp-section">
        <Card title="口径" meta="这一屏不构成投资建议">
          <ul className="qp-caption" style={{ margin: 0, paddingLeft: '1.1em', lineHeight: 1.9 }}>
            <li>风险警示股票已被排除，不进入候选池。</li>
            <li>覆盖率不足时请谨慎解读：未覆盖的权重不计入统计。</li>
            <li>策略变更会使既有报告失效，并要求重新匹配一个已合格的发布版本。</li>
          </ul>
        </Card>
      </section>
    </>
  );
}

function CandidatesTable({
  candidates,
  selected,
  onToggle,
}: {
  candidates: StockView[];
  selected: Set<string>;
  onToggle: (code: string) => void;
}) {
  if (candidates.length === 0) return <EmptyState>这份报告里没有候选。</EmptyState>;
  return (
    <DataTable
      columns={[
        {
          key: '_select',
          header: '采纳',
          width: '70px',
          render: (row) => (
            <input
              type="checkbox"
              checked={selected.has(row.symbol)}
              onChange={() => onToggle(row.symbol)}
              aria-label={`采纳 ${row.symbol}`}
              style={{ width: 18, height: 18 }}
            />
          ),
        },
        { key: 'symbol', header: '股票代码' },
        { key: 'name', header: '名称' },
        {
          key: 'close',
          header: '收盘价',
          numeric: true,
          render: (row) => <span className="qp-num">{formatNumber(row.close)}</span>,
        },
        {
          key: 'change',
          header: '涨跌',
          numeric: true,
          render: (row) => (
            <span className={`qp-num ${changeClass(row.change)}`}>{formatSignedPercent(row.change)}</span>
          ),
        },
        {
          key: 'score',
          header: '模型分数',
          numeric: true,
          render: (row) => <span className="qp-num">{formatNumber(row.score, 4)}</span>,
        },
        {
          key: 'rank',
          header: '模型排名',
          numeric: true,
          render: (row) => (
            <Badge tone="idle" plain>
              #{formatNumber(row.rank, 0)}
            </Badge>
          ),
        },
      ]}
      rows={candidates}
      rowKey={(row) => row.symbol}
      caption="模型排序的候选股"
    />
  );
}

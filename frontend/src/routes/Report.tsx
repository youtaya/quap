/**
 * 研究报告。因子 IC、前瞻收益分位与相对基准的等权组合。
 *
 * 这一屏是**描述性**的：它说的是「模型在历史上表现如何」，不是「明天该买什么」。所以 IC 这类
 * 指标按统计量呈现（带样本数与口径），原始 JSON 收进 L3 折叠区。
 */

import { useState } from 'react';

import type { QlibReport } from '@/api/types';
import { useApi } from '@/api/useApi';
import { DataTable } from '@/components/DataTable';
import { Banner, Button, Card, EmptyState, Facts, LoadingBlock } from '@/components/ui';
import { changeClass, formatMoment, formatNumber, formatSignedPercent } from '@/domain/format';

export function Report() {
  const [target, setTarget] = useState('');
  const report = useApi<QlibReport>(`/qlib-report${target ? `?target=${encodeURIComponent(target)}` : ''}`, [target]);

  return (
    <>
      <div className="qp-heading">
        <h1>研究报告</h1>
        <p>因子 IC、前瞻收益分位与相对基准的等权组合。这是对模型历史表现的描述，不是买卖建议。</p>
      </div>

      <section className="qp-section">
        <Card
          title="报告"
          meta="按数据截点倒序取最新一份"
          actions={
            <div className="qp-row">
              <input
                className="qp-input"
                style={{ width: 180 }}
                placeholder="标的过滤（可留空）"
                value={target}
                onChange={(event) => setTarget(event.target.value)}
                aria-label="标的过滤"
              />
              <Button size="sm" onClick={report.refresh} disabled={report.refreshing}>
                {report.refreshing ? '读取中…' : '重新读取'}
              </Button>
            </div>
          }
        >
          {report.loading ? (
            <LoadingBlock />
          ) : report.error ? (
            <Banner tone="error" title="读不到报告" detail={report.error.detail} />
          ) : !report.data?.report ? (
            <EmptyState>{report.data?.reason ?? '还没有发布 Qlib 研究报告。'}</EmptyState>
          ) : (
            <Facts
              items={[
                { label: '数据截点', value: report.data.as_of ? formatMoment(report.data.as_of) : '—' },
                { label: '引擎', value: report.data.engine ?? '—' },
                { label: '标的', value: report.data.target ?? '全市场' },
                { label: '报告编号', value: report.data.report_id ? String(report.data.report_id) : '—' },
              ]}
            />
          )}
        </Card>
      </section>

      {report.data?.summary ? (
        <>
          <section className="qp-section">
            <h2 style={{ marginBottom: 'var(--space-3)' }}>预测能力</h2>
            <div className="qp-tiles">
              <Tile label="样本数" value={formatNumber(report.data.summary.samples, 0)} />
              <Tile label="IC" value={formatNumber(report.data.summary.ic, 4)} tone={report.data.summary.ic} />
              <Tile label="Rank IC" value={formatNumber(report.data.summary.rank_ic, 4)} tone={report.data.summary.rank_ic} />
              <Tile label="日均 IC" value={formatNumber(report.data.summary.daily_ic, 4)} tone={report.data.summary.daily_ic} />
            </div>
            <p className="qp-caption" style={{ marginTop: 'var(--space-2)' }}>
              IC 是预测值与前瞻收益的相关系数；Rank IC 用排名计算，对极端值更稳健。两者都只描述历史，
              样本外表现需要靠影子观察确认。
            </p>
          </section>

          <section className="qp-section">
            <h2 style={{ marginBottom: 'var(--space-3)' }}>前瞻收益分位</h2>
            <Quantiles value={report.data.summary.quantiles} />
          </section>

          <section className="qp-section">
            <h2 style={{ marginBottom: 'var(--space-3)' }}>相对基准的等权组合</h2>
            <Card title="组合表现" meta="等权、按模型分位构建；不是可交易策略">
              <Facts
                items={[
                  { label: '状态', value: report.data.summary.book.status ?? '—' },
                  { label: '调仓次数', value: formatNumber(report.data.summary.book.rebalances, 0) },
                  { label: '累计收益', value: formatSignedPercent(report.data.summary.book.cumulative_return) },
                  {
                    label: '基准累计收益',
                    value: formatSignedPercent(report.data.summary.book.benchmark_cumulative_return),
                  },
                  { label: '相对收益', value: formatSignedPercent(report.data.summary.book.relative_return) },
                  { label: '平均换手', value: formatNumber(report.data.summary.book.mean_turnover, 4) },
                ]}
              />
            </Card>
          </section>
        </>
      ) : null}

      {report.data?.markdown ? (
        <section className="qp-section">
          <h2 style={{ marginBottom: 'var(--space-3)' }}>报告正文</h2>
          <Card flush>
            <pre
              style={{
                margin: 0,
                padding: 'var(--space-4)',
                whiteSpace: 'pre-wrap',
                overflowWrap: 'anywhere',
                fontSize: '0.8125rem',
                lineHeight: 1.75,
                fontFamily: 'var(--font-sans)',
                maxHeight: 560,
                overflowY: 'auto',
              }}
            >
              {report.data.markdown}
            </pre>
          </Card>
        </section>
      ) : null}

      {report.data?.report ? (
        <section className="qp-section">
          <details className="qp-evidence">
            <summary>原始报告 JSON（L3，仅供核对）</summary>
            <div className="qp-evidence__body">
              <pre
                className="qp-num"
                style={{
                  margin: 0,
                  padding: 'var(--space-3)',
                  background: 'var(--surface-3)',
                  borderRadius: 'var(--radius-md)',
                  fontSize: '0.75rem',
                  maxHeight: 420,
                  overflow: 'auto',
                }}
              >
                {JSON.stringify(report.data.report, null, 2)}
              </pre>
            </div>
          </details>
        </section>
      ) : null}

      <section className="qp-section">
        <Card title="口径" meta="解读这份报告前请先读这三条">
          <ul className="qp-caption" style={{ margin: 0, paddingLeft: '1.1em', lineHeight: 1.9 }}>
            <li>IC 为正说明模型预测方向与未来收益同向；数值很小是正常的，不要按「越大越好」直觉解读。</li>
            <li>分位数组合是**等权**的，用于检验单调性，不代表实际可交易的权重方案。</li>
            <li>报告是描述性的：它不进判决链，也不产生目标权重。目标权重只来自「我的组合」的采纳动作。</li>
          </ul>
        </Card>
      </section>
    </>
  );
}

function Tile({ label, value, tone }: { label: string; value: string; tone?: number | null }) {
  const cls = typeof tone === 'number' ? changeClass(tone) : '';
  return (
    <div className="qp-tile">
      <div className="qp-tile__label">{label}</div>
      <div className={`qp-tile__value ${cls}`}>{value}</div>
    </div>
  );
}

/** 分位表：结构是 `{quantile: value}` 或数组，两种都容错。 */
function Quantiles({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <EmptyState>这份报告没有分位数据。</EmptyState>;
  const rows: { key: string; value: unknown }[] = Array.isArray(value)
    ? value.map((item, index) => ({ key: `Q${index + 1}`, value: item }))
    : Object.entries(value as Record<string, unknown>).map(([key, item]) => ({ key, value: item }));
  if (rows.length === 0) return <EmptyState>这份报告没有分位数据。</EmptyState>;
  return (
    <DataTable
      columns={[
        { key: 'key', header: '分位' },
        {
          key: 'value',
          header: '前瞻收益',
          numeric: true,
          render: (row) => (
            <span className={`qp-num ${changeClass(typeof row.value === 'number' ? row.value : null)}`}>
              {typeof row.value === 'number' ? formatSignedPercent(row.value) : String(row.value)}
            </span>
          ),
        },
      ]}
      rows={rows as unknown as Record<string, unknown>[]}
      rowKey={(row) => String(row['key'])}
      caption="按模型分数分组的平均前瞻收益"
    />
  );
}

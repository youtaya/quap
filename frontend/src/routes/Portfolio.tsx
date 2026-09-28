/**
 * 我的组合。查看基线权重与现金，采纳版本化的 Qlib 建议。
 *
 * 关键约束（来自后端 `VALID_REPORT`）：一份建议只有在「模型已发布 + 未过期 + 策略版本匹配 +
 * 组合版本匹配」时才是有效的。所以这一屏必须把「为什么这份建议不能采纳」说清楚，而不是只给一个
 * 灰掉的按钮。
 */

import { useMemo, useState } from 'react';

import type { ModelPortfolio, RecommendationRow, WatchlistRecord } from '@/api/types';
import { useApi } from '@/api/useApi';
import { DataTable } from '@/components/DataTable';
import { Badge, Banner, Button, Card, EmptyState, Facts, LoadingBlock } from '@/components/ui';
import { useToast } from '@/components/toast';
import { changeClass, formatMoment, formatNumber, formatPercent, formatSignedPercent } from '@/domain/format';
import { ACTIONS, FREQUENCIES, label } from '@/domain/labels';
import type { StockView } from '@/api/types';

export function Portfolio() {
  const portfolios = useApi<ModelPortfolio[]>('/model-portfolios');
  const watchlist = useApi<WatchlistRecord>('/watchlist');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [frequency, setFrequency] = useState<'day' | '5min'>('day');
  const toast = useToast();

  const active = useMemo(() => {
    if (!portfolios.data || portfolios.data.length === 0) return null;
    return portfolios.data.find((item) => item.id === selectedId) ?? portfolios.data[0]!;
  }, [portfolios.data, selectedId]);

  const reports = useApi<RecommendationRow[]>(
    active ? `/recommendations?frequency=${frequency}&portfolio_id=${active.id}&valid_only=false` : null,
    [active?.id, frequency],
  );

  const report = reports.data?.[0] ?? null;

  return (
    <>
      <div className="qp-heading">
        <h1>我的组合</h1>
        <p>查看基线权重与现金，采纳版本化的 Qlib 建议。本平台只给建议，不发送任何委托。</p>
      </div>

      <Banner
        tone="info"
        title="采纳建议 = 生成一个新的组合版本"
        detail="采纳不会下单。它会按你勾选的调整生成一个带版本号的基线，供下一个交易日使用。"
      />

      <section className="qp-section">
        <Card
          title="组合"
          meta="基线权重与现金比例之和必须等于 1"
          actions={
            <div className="qp-row" role="radiogroup" aria-label="建议频率">
              {(['day', '5min'] as const).map((item) => (
                <Button
                  key={item}
                  size="sm"
                  variant={frequency === item ? 'primary' : 'default'}
                  role="radio"
                  aria-checked={frequency === item}
                  onClick={() => setFrequency(item)}
                >
                  {FREQUENCIES[item]}
                </Button>
              ))}
            </div>
          }
        >
          {portfolios.loading ? (
            <LoadingBlock />
          ) : portfolios.error ? (
            <Banner tone="error" title="读不到组合" detail={portfolios.error.detail} />
          ) : portfolios.data && portfolios.data.length > 0 ? (
            <>
              <div className="qp-row" style={{ marginBottom: 'var(--space-4)' }}>
                {portfolios.data.map((item) => (
                  <Button
                    key={item.id}
                    size="sm"
                    variant={active?.id === item.id ? 'primary' : 'default'}
                    onClick={() => setSelectedId(item.id)}
                    aria-pressed={active?.id === item.id}
                  >
                    {item.name} · v{item.revision}
                  </Button>
                ))}
              </div>
              {active ? (
                <Facts
                  items={[
                    { label: '组合名称', value: active.name },
                    { label: '版本', value: `v${active.revision}` },
                    { label: '现金比例', value: formatPercent(active.cash_weight) },
                    { label: '持仓只数', value: formatNumber(Object.keys(active.weights ?? {}).length, 0) },
                    { label: '生效自', value: active.effective_from ? formatMoment(active.effective_from) : '—' },
                  ]}
                />
              ) : null}
            </>
          ) : (
            <EmptyState>还没有模型组合。请先在「运行记录与配置」确认数据就绪，再创建组合。</EmptyState>
          )}
        </Card>
      </section>

      <section className="qp-section">
        <h2 style={{ marginBottom: 'var(--space-3)' }}>最新建议</h2>
        {!active ? (
          <EmptyState>先选一个组合。</EmptyState>
        ) : reports.loading ? (
          <LoadingBlock />
        ) : reports.error ? (
          <Banner tone="error" title="读不到建议" detail={reports.error.detail} />
        ) : !report ? (
          <EmptyState>这个组合在{FREQUENCIES[frequency]}上还没有任何建议。</EmptyState>
        ) : (
          <>
            {!report.valid ? (
              <Banner
                tone="warn"
                title="这份建议当前无效，不能采纳"
                detail="有效条件：模型已发布且未过期、策略版本为最新、组合版本与建议一致。到「今日」查看是哪一环没通过。"
              />
            ) : (
              <Banner
                tone="ok"
                title="这份建议有效"
                detail={`可用至 ${formatMoment(report.valid_until)}；过期后需要重新推理。`}
              />
            )}
            <Facts
              items={[
                { label: '数据截点', value: formatMoment(report.as_of) },
                { label: '可用时刻', value: formatMoment(report.available_at) },
                { label: '有效至', value: formatMoment(report.valid_until) },
                { label: '策略版本', value: `v${report.policy_revision}` },
                { label: '建议现金比例', value: formatPercent(report.data.cash_weight) },
              ]}
            />
            <div style={{ marginTop: 'var(--space-4)' }}>
              <StockTable stocks={report.data.stocks ?? []} />
            </div>
          </>
        )}
      </section>

      <section className="qp-section">
        <h2 style={{ marginBottom: 'var(--space-3)' }}>观察池</h2>
        <Card title="当前观察池" meta="供下一交易日使用；与持仓无关">
          {watchlist.loading ? (
            <LoadingBlock />
          ) : watchlist.data && watchlist.data.symbols.length > 0 ? (
            <>
              <p className="qp-caption">版本 v{watchlist.data.revision} · 共 {watchlist.data.symbols.length} 只</p>
              <div className="qp-row">
                {watchlist.data.symbols.slice(0, 60).map((code) => (
                  <Badge key={code} tone="idle" plain>
                    {code}
                  </Badge>
                ))}
                {watchlist.data.symbols.length > 60 ? (
                  <span className="qp-caption">…以及另外 {watchlist.data.symbols.length - 60} 只</span>
                ) : null}
              </div>
            </>
          ) : (
            <EmptyState>观察池是空的。</EmptyState>
          )}
        </Card>
      </section>

      <section className="qp-section">
        <Card title="口径" meta="这一屏只读；采纳入口在「选股发现」与建议卡内">
          <ul className="qp-caption" style={{ margin: 0, paddingLeft: '1.1em', lineHeight: 1.9 }}>
            <li>权重是<b>目标权重</b>，不是市值权重：基线里写的是下一交易日希望持有的比例。</li>
            <li>现金比例是显式字段，不会被静默归一化——权重与现金之和必须精确等于 1。</li>
            <li>「无效」的建议不会被隐藏，但会标明原因：隐藏会让人以为平台忘了跑。</li>
          </ul>
        </Card>
      </section>

      <p className="qp-caption" style={{ marginTop: 'var(--space-5)' }}>
        <button
          type="button"
          className="qp-btn qp-btn--quiet qp-btn--sm"
          onClick={() => {
            portfolios.refresh();
            reports.refresh();
            toast.push('info', '已重新读取', '组合与建议都会刷新。');
          }}
        >
          重新读取
        </button>
      </p>
    </>
  );
}

function StockTable({ stocks }: { stocks: StockView[] }) {
  if (stocks.length === 0) return <EmptyState>这份建议里没有个股。</EmptyState>;
  return (
    <DataTable
      columns={[
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
        { key: 'rank', header: '排名', numeric: true },
        {
          key: 'baseline_weight',
          header: '基线权重',
          numeric: true,
          render: (row) => <span className="qp-num">{formatPercent(row.baseline_weight)}</span>,
        },
        {
          key: 'target_weight',
          header: '目标权重',
          numeric: true,
          render: (row) => <span className="qp-num">{formatPercent(row.target_weight)}</span>,
        },
        {
          key: 'action',
          header: '动作',
          render: (row) => (
            <Badge tone={row.action === 'exit' || row.action === 'reduce' ? 'warn' : row.action === 'hold' ? 'idle' : 'info'} plain>
              {label(ACTIONS, row.action)}
            </Badge>
          ),
        },
      ]}
      rows={stocks as unknown as Record<string, unknown>[]}
      rowKey={(row) => String(row['symbol'])}
      caption="建议里的个股明细"
    />
  );
}

/**
 * 个股研究。查看行情与模型视角，**不代表券商持仓**。
 *
 * 这一屏最容易出现的误读是：把「模型分数」当成「推荐买入」。所以模型视角与行情并列呈现，
 * 并明确写出「这只是模型在某一截点上的排序，不是持仓建议」。
 */

import { useEffect, useState } from 'react';

import type { Instrument, Page, Quote, ScorePoint, StockState } from '@/api/types';
import { useApi } from '@/api/useApi';
import { DataTable } from '@/components/DataTable';
import { Badge, Banner, Button, Card, EmptyState, Facts, LoadingBlock } from '@/components/ui';
import { changeClass, formatMoment, formatNumber, formatPercent, formatSignedPercent, text } from '@/domain/format';
import { ACTIONS, BOARDS, FREQUENCIES, label } from '@/domain/labels';

export function StockResearch() {
  const [search, setSearch] = useState('');
  const [debounced, setDebounced] = useState('');
  const [code, setCode] = useState('');
  const [frequency, setFrequency] = useState<'day' | '5min'>('day');

  // 输入防抖：每敲一个字就打一次接口会把 500 条证券目录的查询变成打字游戏。
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(search.trim()), 300);
    return () => window.clearTimeout(timer);
  }, [search]);

  const instruments = useApi<Instrument[]>(`/instruments?search=${encodeURIComponent(debounced)}`, [debounced]);
  const quote = useApi<Quote[]>(code ? `/quotes?code=${code}` : null, [code]);
  const view = useApi<StockState>(code ? `/recommendations/stock/${code}?frequency=${frequency}` : null, [code, frequency]);
  const scores = useApi<Page<ScorePoint>>(
    code ? `/research-stocks/${code}/scores?frequency=${frequency}&limit=60` : null,
    [code, frequency],
  );

  const row = quote.data?.[0];
  const data = (row?.data ?? {}) as Record<string, unknown>;

  return (
    <>
      <div className="qp-heading">
        <h1>个股研究</h1>
        <p>查看行情与模型视角，不代表券商持仓。这一屏不产生目标权重，也不会触发任何委托。</p>
      </div>

      <Banner
        tone="info"
        title="模型视角 ≠ 持仓建议"
        detail="「模型分数」是模型在某个数据截点上给这只股票的打分与排名。它不包含估值、基本面或你的持仓约束。"
      />

      <section className="qp-section">
        <Card title="搜索股票" meta="按代码或名称匹配，最多返回 500 条">
          <div className="qp-row">
            <input
              className="qp-input"
              style={{ maxWidth: 320 }}
              placeholder="输入代码或名称，例如 600519 或 贵州茅台"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              aria-label="搜索股票"
            />
            <span className="qp-caption">
              {debounced
                ? `匹配到 ${instruments.data?.length ?? 0} 条`
                : '留空时列出全部证券目录'}
            </span>
          </div>

          {instruments.data && instruments.data.length > 0 ? (
            <div className="qp-row" style={{ marginTop: 'var(--space-4)' }}>
              {instruments.data.slice(0, 40).map((item) => (
                <Button
                  key={item.symbol}
                  size="sm"
                  variant={code === item.symbol ? 'primary' : 'default'}
                  onClick={() => setCode(item.symbol)}
                  aria-pressed={code === item.symbol}
                >
                  {item.symbol} {item.name}
                </Button>
              ))}
              {instruments.data.length > 40 ? (
                <span className="qp-caption">…以及另外 {instruments.data.length - 40} 条，请细化搜索词</span>
              ) : null}
            </div>
          ) : debounced && !instruments.loading ? (
            <EmptyState>没有匹配的证券。</EmptyState>
          ) : null}
        </Card>
      </section>

      {!code ? (
        <EmptyState>先选一只股票。</EmptyState>
      ) : (
        <>
          <section className="qp-section">
            <Card
              title={`行情 · ${code}`}
              meta={row ? `数据时间 ${formatMoment(data['source_time'])}` : '正在读取'}
              actions={
                row ? (
                  <Badge tone={row.fresh ? 'ok' : 'warn'} plain>
                    {row.fresh ? '行情新鲜' : '行情已过期'}
                  </Badge>
                ) : null
              }
            >
              {quote.loading ? (
                <LoadingBlock />
              ) : !row ? (
                <EmptyState>没有这只股票的行情快照。</EmptyState>
              ) : (
                <Facts
                  items={[
                    {
                      label: '最新价',
                      value: (
                        <span className={`qp-num ${changeClass(data['change'])}`}>
                          {formatNumber(data['close'])}
                        </span>
                      ),
                    },
                    {
                      label: '涨跌',
                      value: (
                        <span className={`qp-num ${changeClass(data['change'])}`}>
                          {formatSignedPercent(data['change'])}
                        </span>
                      ),
                    },
                    { label: '今开', value: <span className="qp-num">{formatNumber(data['open'])}</span> },
                    { label: '最高', value: <span className="qp-num">{formatNumber(data['high'])}</span> },
                    { label: '最低', value: <span className="qp-num">{formatNumber(data['low'])}</span> },
                    { label: '成交额', value: <span className="qp-num">{formatNumber(data['turnover'], 0)}</span> },
                  ]}
                />
              )}
            </Card>
          </section>

          <section className="qp-section">
            <Card
              title="模型视角"
              meta="按频率分别判定；无效报告不会给出模型分数"
              actions={
                <div className="qp-row" role="radiogroup" aria-label="频率">
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
              {view.loading ? (
                <LoadingBlock />
              ) : view.error ? (
                <Banner tone="error" title="读不到模型视角" detail={view.error.detail} />
              ) : view.data?.state === 'unavailable' ? (
                <Banner
                  tone="warn"
                  title="模型暂时给不出这只股票的视角"
                  detail={
                    view.data.reason === 'No valid Qlib report'
                      ? '今天还没有一份有效的 Qlib 报告。到「今日」查看是哪一环没通过。'
                      : '这只股票不在当前特征或预测覆盖范围内。'
                  }
                />
              ) : view.data?.model_view ? (
                <>
                  <Facts
                    items={[
                      {
                        label: '模型分数',
                        value: <span className="qp-num">{formatNumber(view.data.model_view.score, 4)}</span>,
                      },
                      {
                        label: '模型排名',
                        value: <span className="qp-num">#{formatNumber(view.data.model_view.rank, 0)}</span>,
                      },
                      { label: '数据截点', value: view.data.as_of ? formatMoment(view.data.as_of) : '—' },
                      { label: '可用时刻', value: view.data.available_at ? formatMoment(view.data.available_at) : '—' },
                      { label: '有效至', value: view.data.valid_until ? formatMoment(view.data.valid_until) : '—' },
                      {
                        label: '组合动作',
                        value: view.data.model_view.action
                          ? label(ACTIONS, view.data.model_view.action)
                          : '未绑定组合',
                      },
                    ]}
                  />
                  <p className="qp-caption" style={{ marginTop: 'var(--space-3)' }}>
                    分数与排名是模型在<b>单一截点</b>上的输出。它没有考虑你的持仓、成本与风险偏好，
                    也不构成买卖建议。
                  </p>
                </>
              ) : (
                <EmptyState>没有模型视角数据。</EmptyState>
              )}
            </Card>
          </section>

          <section className="qp-section">
            <h2 style={{ marginBottom: 'var(--space-3)' }}>模型分数历史</h2>
            <DataTable
              columns={[
                { key: 'feature_cutoff', header: '特征截点' },
                { key: 'observation_time', header: '观测时刻' },
                {
                  key: 'score',
                  header: '分数',
                  numeric: true,
                  render: (row2) => <span className="qp-num">{formatNumber(row2.score, 4)}</span>,
                },
                {
                  key: 'rank',
                  header: '排名',
                  numeric: true,
                  render: (row2) => <span className="qp-num">#{formatNumber(row2.rank, 0)}</span>,
                },
              ]}
              rows={(scores.data?.items ?? []) as unknown as Record<string, unknown>[]}
              rowKey={(row2) => String(row2['id'])}
              empty="这只股票还没有模型分数记录。"
              caption="历史模型分数"
            />
            <p className="qp-caption" style={{ marginTop: 'var(--space-2)' }}>
              历史分数是<b>事后</b>的：它们只用于检验模型，不可用于回测交易或作为当时的决策依据。
            </p>
          </section>

          {instruments.data ? (
            <section className="qp-section">
              <Card title="证券目录信息" meta="来自交易所证券目录">
                <Facts
                  items={[
                    {
                      label: '板块',
                      value: label(
                        BOARDS,
                        instruments.data.find((item) => item.symbol === code)?.board ?? '',
                      ),
                    },
                    { label: '名称', value: text(instruments.data.find((item) => item.symbol === code)?.name) },
                    { label: '状态', value: text(instruments.data.find((item) => item.symbol === code)?.status) },
                    {
                      label: '上市日',
                      value: text(instruments.data.find((item) => item.symbol === code)?.list_date),
                    },
                    { label: '持仓占比', value: '—（本平台不持有仓位）' },
                    { label: '覆盖权重', value: formatPercent(null) },
                  ]}
                />
              </Card>
            </section>
          ) : null}
        </>
      )}
    </>
  );
}

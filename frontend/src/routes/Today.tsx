/**
 * 今日 —— 判决面。这是整个重构的落点。
 *
 * 它取代了原先的三次往返：改造前要回答「今天能不能出建议」，操作员必须横跨
 * Overview（看到 blocked）→ Data & Pipeline（找原因）→ Models & Validation（训练）→ 再回 Overview。
 * 现在这一屏就给出结论、原因、以及**此刻该做的那一件事**。
 *
 * 数据来源只有两个：`/data-readiness`（判决与门禁链，侧边栏共用同一份）和 `/qlib-generations`
 * （L3 证据）。判决句本身由 `domain/verdict.ts` 组稿，本文件只负责排布。
 */

import { useMemo, useState } from 'react';

import { request } from '@/api/client';
import type { Generation, Readiness } from '@/api/types';
import { useApi } from '@/api/useApi';
import { DataTable } from '@/components/DataTable';
import { GateCard } from '@/components/GateCard';
import { Badge, Banner, Button, Card, EmptyState, Facts, LoadingBlock } from '@/components/ui';
import { useToast } from '@/components/toast';
import { formatDay, formatMoment, formatNumber } from '@/domain/format';
import { FREQUENCIES, ORIGINS, label } from '@/domain/labels';
import { composeVerdict, fullChain, gateChainOf, overlayNote } from '@/domain/verdict';
import type { GateAction } from '@/domain/verdict';
import { useReadiness } from '@/shell/readiness';

type Frequency = 'day' | '5min';

export function Today() {
  const readiness = useReadiness();
  const generations = useApi<Generation[]>('/qlib-generations');
  const toast = useToast();
  const [frequency, setFrequency] = useState<Frequency>('day');
  const [pending, setPending] = useState<string | null>(null);

  /**
   * 判决面所需的一切都在这一个 memo 里算完，而且**先验契约再算**：`gateChainOf` 返回 null 时
   * 不去调 `composeVerdict`，否则版本错配会直接抛异常变成白屏。
   */
  const view = useMemo(() => {
    const data = readiness.data;
    if (!data) return null;
    const chain = gateChainOf(data, frequency);
    if (!chain) return { chain: null, verdict: null, stages: [] as ReturnType<typeof fullChain> };
    return { chain, verdict: composeVerdict(data, frequency), stages: fullChain(data, frequency) };
  }, [readiness.data, frequency]);

  // `verdict` 故意**不在这里**取。它要等两个前置判断通过之后再解引用，见下方注释。
  const stages = view?.stages ?? [];

  /** 门禁的显示名，用于把「等待上游」说清楚——只说「等待」等于没说。 */
  const labelOf = useMemo(() => {
    const map = new Map<string, string>();
    for (const stage of stages) {
      for (const item of stage.gates) map.set(item.gate.key, item.label);
    }
    return map;
  }, [stages]);

  async function submit(spec: NonNullable<GateAction['request']>, successTitle: string) {
    setPending(successTitle);
    // `request_key` 在**点击这一刻**生成：后端用它去重，复用同一个键会让「失败后再试一次」
    // 拿回上一次的运行而不是新开一次。
    const body =
      spec.body && typeof spec.body === 'object' && 'request_key' in spec.body
        ? { ...(spec.body as Record<string, unknown>), request_key: crypto.randomUUID() }
        : spec.body;
    try {
      const result = await request<Record<string, unknown>>(spec.path, { method: spec.method, body });
      const runId = typeof result?.['run_id'] === 'string' ? result['run_id'] : null;
      toast.push(
        'ok',
        successTitle,
        runId ? `任务 ${runId} 已提交。进度见「运行记录与配置」。` : '指令已受理，进度见「运行记录与配置」。',
      );
    } catch (failure) {
      const detail = failure instanceof Error ? failure.message : '未知错误';
      toast.push('error', '提交失败', detail);
    } finally {
      setPending(null);
    }
  }

  function recheck() {
    readiness.refresh();
    toast.push('info', '已重新检测', '判决条与侧边栏会同时更新。');
  }

  if (readiness.loading) {
    return (
      <>
        <div className="qp-heading">
          <h1>今日</h1>
          <p>今天能不能出建议，以及为什么。</p>
        </div>
        <LoadingBlock label="正在读取判决…" />
      </>
    );
  }

  if (readiness.error) {
    return (
      <>
        <div className="qp-heading">
          <h1>今日</h1>
          <p>今天能不能出建议，以及为什么。</p>
        </div>
        <Banner
          tone="error"
          title="读不到判决"
          detail={readiness.error.detail}
          actions={
            <>
              <Button variant="primary" onClick={recheck} disabled={readiness.refreshing}>
                {readiness.refreshing ? '正在重新检测…' : '重新检测'}
              </Button>
              {readiness.error.kind === 'unauthorized' ? (
                <span className="qp-caption">令牌可能已失效，请退出后重新登录。</span>
              ) : null}
            </>
          }
        />
      </>
    );
  }

  if (!readiness.data) {
    return <EmptyState>没有拿到判决数据。</EmptyState>;
  }

  // 版本错配要明说。旧版 API 没有 `gates`，此时判决面无法渲染链路——白屏是最糟的失败形态，
  // 它连「哪里不对」都不说。
  //
  // 这一条**必须排在 `!verdict` 之前**：旧版 API 下 `readiness.data` 有值、`view.chain` 为 null，
  // 于是 `verdict` 也必然是 null。若先判 `!verdict`，操作员看到的是「没有拿到判决数据」——
  // 一句什么都没解释的话，而真正的原因是前后端版本错配，那是他自己就能修好的。
  if (!view?.chain) {
    return (
      <>
        <div className="qp-heading">
          <h1>今日</h1>
          <p>今天能不能出建议，以及为什么。</p>
        </div>
        <Banner
          tone="error"
          title="前端与后端版本不一致"
          detail="接口返回的判决结构里没有门禁链（gates 字段）。这说明 api 服务还是旧版本，而前端是新版本。请一并重建 api 与 web，不要只重建其中一个。"
          actions={
            <Button onClick={recheck} disabled={readiness.refreshing}>
              {readiness.refreshing ? '正在重新检测…' : '重新检测'}
            </Button>
          }
        />
      </>
    );
  }

  // 走到这里，契约已经验过：`view.chain` 非空 ⇒ `view` 被收窄到「链存在」那一支，
  // 而该支的 `verdict` 由 `composeVerdict` 直接产出，类型是 `Verdict` 而非 `Verdict | null`。
  const verdict = view.verdict;
  const data: Readiness = readiness.data;
  const overlay = data.frequencies[frequency === 'day' ? '5min' : 'day'];
  const state = data.frequencies[frequency];

  const dayGates = data.frequencies.day?.gates ?? [];
  const capabilityGate = dayGates.find((gate) => gate.key === 'capability');
  const generationGate = dayGates.find((gate) => gate.key === 'generation');
  const modelGate = dayGates.find((gate) => gate.key === 'model');

  return (
    <>
      <div className="qp-heading">
        <h1>今日</h1>
        <p>今天能不能出建议，以及为什么。下面按依赖顺序摊开每一个环节，卡在哪一环就在哪一环处理。</p>
      </div>

      {/* ── 频率切换。日线是主判决；五分钟是叠加，默认不干扰。 ── */}
      <div className="qp-row" style={{ marginBottom: 'var(--space-4)' }} role="radiogroup" aria-label="频率">
        {(['day', '5min'] as Frequency[]).map((item) => (
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
        <span className="qp-caption">日线是主判决；五分钟是叠加，受阻不影响日线。</span>
      </div>

      {/* ── 判决条 ── */}
      <section className={`qp-verdict qp-verdict--${verdict.tone}`} aria-label="今日判决">
        <div className="qp-verdict__top">
          <div className="qp-verdict__icon" aria-hidden="true">
            {verdict.tone === 'ok' ? '✓' : verdict.tone === 'error' ? '✕' : '◐'}
          </div>
          <div className="qp-verdict__body">
            <div className="qp-verdict__eyebrow">{verdict.eyebrow}</div>
            <h2 className="qp-verdict__sentence">{verdict.headline}</h2>
            <p className="qp-verdict__reason">{verdict.detail}</p>

            {verdict.action ? (
              <div className="qp-verdict__action">
                <Button
                  variant="primary"
                  disabled={pending !== null}
                  onClick={() =>
                    verdict.action?.request &&
                    submit(verdict.action.request, verdict.action.label)
                  }
                >
                  {pending ?? verdict.action.label}
                </Button>
                <span className="qp-verdict__note">{verdict.action.hint}</span>
              </div>
            ) : (
              <div className="qp-verdict__action">
                <Button onClick={recheck} disabled={readiness.refreshing}>
                  {readiness.refreshing ? '正在重新检测…' : '重新检测'}
                </Button>
                <span className="qp-verdict__note">
                  建议已就绪，无需处理。过期后本页会提示重新推理。
                </span>
              </div>
            )}
          </div>
        </div>

        <div className="qp-verdict__foot">
          <Badge tone={overlay?.ready ? (overlay.output.published ? 'ok' : 'idle') : 'warn'} plain>
            {overlay ? overlayNote(overlay) : '另一频率状态未知'}
          </Badge>
          <span>
            两条频率的判决是解耦的：五分钟受阻不会拖住日线建议的解锁，反之亦然。
          </span>
        </div>
      </section>

      {/* ── 四个磁贴：L1 结论层的数字，只留能改变判断的那几个 ── */}
      <div className="qp-tiles" style={{ marginBottom: 'var(--space-5)' }}>
        <Tile
          label="数据源能力"
          value={
            capabilityGate?.status === 'passed'
              ? '全部已验证'
              : `${capabilityGate?.evidence.missing_count ?? '—'} 项待验证`
          }
        />
        <Tile
          label="数据代次"
          value={generationGate?.status === 'passed' ? '已就绪' : generationGate?.status === 'blocked' ? '缺失' : '等待上游'}
        />
        <Tile
          label="模型"
          value={modelGate?.status === 'passed' ? '已发布' : modelGate?.status === 'blocked' ? '无可用模型' : '等待上游'}
        />
        <Tile
          label="今日建议"
          value={
            state.output.published
              ? '已出'
              : verdict.ready
                ? '可出，尚未生成'
                : '出不了'
          }
        />
      </div>

      {/* ── 解锁链路 ── */}
      <section className="qp-section">
        <h2 style={{ marginBottom: 'var(--space-3)' }}>解锁链路</h2>
        <div className="qp-chain">
          {stages.map((stage) => (
            <div key={stage.stage} className={`qp-stage qp-stage--${stage.dependency}`}>
              <div className="qp-stage__head">
                <span className="qp-stage__title">{stage.label}</span>
                <span className="qp-stage__dep">
                  {stage.dependency === 'independent'
                    ? `${stage.gates.length} 项互相独立 · 可以同时处理`
                    : '顺序依赖 · 上一环不通过时，下一环的状态无从判断'}
                </span>
              </div>
              <div className="qp-stage__gates">
                {stage.gates.map((view) => (
                  <GateCard
                    key={view.gate.key}
                    view={view}
                    upstreamLabels={view.gate.depends_on.map((key) => labelOf.get(key) ?? key)}
                    onRunAction={(action) => {
                      if (action.request) submit(action.request, action.label);
                    }}
                    actionPending={pending === view.action.label}
                    onRecheck={recheck}
                    rechecking={readiness.refreshing}
                  />
                ))}
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* ── L3：证据 ── */}
      <section className="qp-section">
        <h2 style={{ marginBottom: 'var(--space-3)' }}>证据</h2>
        <div className="qp-stack">
          <details className="qp-evidence">
            <summary>数据源能力明细（{data.capabilities.length} 项）</summary>
            <div className="qp-evidence__body">
              <DataTable
                columns={[
                  { key: 'endpoint', header: '接口' },
                  { key: 'status', header: '状态' },
                  { key: 'updated_at', header: '最近更新' },
                  {
                    key: 'schema_verified',
                    header: '契约已校验',
                    render: (row) => ((row.data ?? {}) as Record<string, unknown>)['schema_verified'] ? '是' : '否',
                  },
                ]}
                rows={data.capabilities as unknown as Record<string, unknown>[]}
                rowKey={(row, index) => `${String(row['endpoint'])}-${index}`}
                empty="还没有任何数据源能力记录。"
                caption="数据源能力与契约校验状态"
              />
            </div>
          </details>

          <details className="qp-evidence">
            <summary>不可变数据代次（{data.generations.length} 条）</summary>
            <div className="qp-evidence__body">
              <DataTable
                columns={[
                  { key: 'id', header: '代次' },
                  { key: 'frequency', header: '频率' },
                  { key: 'created_at', header: '创建时刻' },
                  { key: 'origin', header: '来源', render: (row) => label(ORIGINS, row.origin ?? 'pipeline') },
                ]}
                rows={generations.data as unknown as Record<string, unknown>[]}
                rowKey={(row) => String(row['id'])}
                empty="还没有通过验证的数据代次。"
                caption="不可变 Qlib 数据代次"
              />
            </div>
          </details>

          <details className="qp-evidence">
            <summary>采集与容量参数</summary>
            <div className="qp-evidence__body">
              <Facts
                items={[
                  { label: '引擎版本', value: data.engine },
                  { label: '历史采集交易日', value: formatNumber(data.history_sessions, 0) },
                  { label: '因子交易日', value: formatNumber(data.factor_sessions, 0) },
                  { label: '分钟线交易日', value: formatNumber(data.minute_history_sessions, 0) },
                  { label: '盘中池容量', value: formatNumber(data.pool_capacity, 0) },
                ]}
              />
            </div>
          </details>
        </div>
      </section>

      {/* ── 免责与口径 ── */}
      <Card title="口径与免责" meta="这一屏的结论只覆盖日线与五分钟两条 Qlib 链路">
        <ul className="qp-caption" style={{ margin: 0, paddingLeft: '1.1em', lineHeight: 1.9 }}>
          <li>红涨绿跌，时间为北京时间。本平台只做研究与建议，<b>不执行任何交易</b>。</li>
          <li>
            数据代次是不可变的：同一代次重跑推理会得到同一结果，所以「重试」用的是相同输入，不是重新采集。
          </li>
          <li>
            判决只认「已评估通过 + 已批准 + 未过期」的模型；影子观察中的候选模型不参与判决。
          </li>
        </ul>
      </Card>

      {state.blockers.length > 0 ? (
        <details className="qp-evidence" style={{ marginTop: 'var(--space-5)' }}>
          <summary>后端原始 blocker 字符串（{state.blockers.length} 条，仅供核对）</summary>
          <div className="qp-evidence__body">
            <Banner
              tone="info"
              title="这些字符串是机器可读原因，不是界面文案"
              detail="判决条与门禁卡上的句子由前端按门禁 key 组稿，数字取自 evidence。这里原样列出，只为在怀疑组稿出错时有个对照。"
            />
            <DataTable
              columns={[{ key: 'reason', header: '原始字符串' }]}
              rows={state.blockers.map((reason) => ({ reason }))}
              rowKey={(row) => row.reason}
              caption="后端原始 blocker"
            />
          </div>
        </details>
      ) : null}

      <p className="qp-caption" style={{ marginTop: 'var(--space-5)' }}>
        判决读取于 {formatMoment(new Date().toISOString())} · 数据代次最新一条 {formatDay(data.generations[0]?.created_at)}
      </p>
    </>
  );
}

function Tile({ label: text, value }: { label: string; value: string }) {
  return (
    <div className="qp-tile">
      <div className="qp-tile__label">{text}</div>
      <div className="qp-tile__value">{value}</div>
    </div>
  );
}

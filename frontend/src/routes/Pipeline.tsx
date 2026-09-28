/**
 * 运行记录与配置。**只读**为主：能改变判决的动作只能从判决面发起（附录 A.6）。
 *
 * 所以这里保留的是「追溯」与「诊断」：不可变代次、流水线运行、依赖关系、定向重试。
 * 采集控制（立即采集／检查数据源）保留在这里，因为它是诊断工具而不是判决动作——但页面上
 * 明确标注它与判决的关系，避免操作员以为「点一下就能解锁」。
 */

import { useState } from 'react';

import { api } from '@/api/client';
import type { Generation, OperationsStatus, PipelineRun, PolicyRecord, RuntimeSettings } from '@/api/types';
import { useApi } from '@/api/useApi';
import { DataTable } from '@/components/DataTable';
import { Badge, Banner, Button, Card, Facts, LoadingBlock } from '@/components/ui';
import { useToast } from '@/components/toast';
import { formatAge, formatDay, formatInteger, formatNumber, summarize } from '@/domain/format';
import { FREQUENCIES, ORIGINS, PURPOSES, ROLES, label } from '@/domain/labels';

export function Pipeline() {
  const runs = useApi<PipelineRun[]>('/pipeline-runs');
  const generations = useApi<Generation[]>('/qlib-generations');
  const policy = useApi<PolicyRecord>('/scan-policy');
  const settings = useApi<RuntimeSettings>('/settings');
  const status = useApi<OperationsStatus>('/status');
  const toast = useToast();
  const [busy, setBusy] = useState<string | null>(null);

  async function retry(runId: string) {
    setBusy(runId);
    try {
      await api.post(`/pipeline-runs/${runId}/retry`);
      toast.push('ok', '已提交重试', `运行 ${runId} 用相同输入重新排队。`);
      runs.refresh();
    } catch (failure) {
      toast.push('error', '重试失败', failure instanceof Error ? failure.message : '未知错误');
    } finally {
      setBusy(null);
    }
  }

  async function control(action: string, title: string) {
    setBusy(action);
    try {
      await api.post('/control', { action });
      toast.push('ok', title, '后台执行，离开页面不影响任务。');
      status.refresh();
    } catch (failure) {
      toast.push('error', `${title}失败`, failure instanceof Error ? failure.message : '未知错误');
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      <div className="qp-heading">
        <h1>运行记录与配置</h1>
        <p>追溯前置条件、不可变代次、依赖关系与定向重试。这里不改判决——要改判决请去「今日」。</p>
      </div>

      <Banner
        tone="info"
        title="这个空间是只读的，除了两处例外"
        detail="「立即采集」与「检查数据源」是诊断工具，它们重新探测数据源，但不会替你解开判决——判决是否解开由「今日」页重新检测后判定。"
      />

      <section className="qp-section">
        <Card title="采集控制" meta="诊断工具，不是判决动作">
          <div className="qp-row">
            {[
              { action: 'refresh', title: '立即采集' },
              { action: 'doctor', title: '检查数据源' },
              { action: 'pause', title: '暂停采集' },
              { action: 'resume', title: '恢复采集' },
            ].map((item) => (
              <Button
                key={item.action}
                disabled={busy !== null}
                onClick={() => control(item.action, item.title)}
              >
                {busy === item.action ? '已提交…' : item.title}
              </Button>
            ))}
          </div>
        </Card>
      </section>

      <section className="qp-section">
        <h2 style={{ marginBottom: 'var(--space-3)' }}>流水线运行</h2>
        {runs.loading ? (
          <LoadingBlock />
        ) : runs.error ? (
          <Banner tone="error" title="读不到运行记录" detail={runs.error.detail} />
        ) : (
          <DataTable
            columns={[
              { key: 'id', header: '运行', width: '120px' },
              { key: 'frequency', header: '频率', width: '80px' },
              { key: 'purpose', header: '用途', width: '90px' },
              { key: 'as_of', header: '数据截点' },
              { key: 'state', header: '状态', width: '90px' },
              { key: 'created_at', header: '创建时刻' },
              { key: 'error', header: '错误', render: (row) => (row.error ? String(row.error) : '—') },
              {
                key: 'result',
                header: '结果',
                render: (row) => <span className="qp-caption">{summarize(row.result)}</span>,
              },
              {
                key: '_retry',
                header: '操作',
                render: (row) =>
                  row.state === 'failed' ? (
                    <Button size="sm" disabled={busy !== null} onClick={() => retry(String(row.id))}>
                      {busy === row.id ? '已提交…' : '用相同输入重试'}
                    </Button>
                  ) : (
                    <span className="qp-muted">—</span>
                  ),
              },
            ]}
            rows={runs.data as unknown as Record<string, unknown>[]}
            rowKey={(row) => String(row['id'])}
            empty="还没有流水线运行记录。"
            caption="最近的流水线运行"
          />
        )}
        <p className="qp-caption" style={{ marginTop: 'var(--space-2)' }}>
          「用相同输入重试」不会重新采集：数据代次是不可变的，重试得到的是同一份输入的同一份结果。
        </p>
      </section>

      <section className="qp-section">
        <h2 style={{ marginBottom: 'var(--space-3)' }}>不可变 Qlib 数据代次</h2>
        <DataTable
          columns={[
            { key: 'id', header: '代次' },
            { key: 'frequency', header: '频率', width: '80px' },
            { key: 'created_at', header: '创建时刻' },
            { key: 'origin', header: '来源', render: (row) => label(ORIGINS, row.origin ?? 'pipeline') },
          ]}
          rows={generations.data as unknown as Record<string, unknown>[]}
          rowKey={(row) => String(row['id'])}
          empty="还没有通过验证的数据代次。"
          caption="不可变 Qlib 数据代次"
        />
      </section>

      <section className="qp-section">
        <h2 style={{ marginBottom: 'var(--space-3)' }}>服务与接口</h2>
        <div className="qp-grid">
          <Card title="工作进程心跳" meta="20 秒内有心跳才算健康">
            {status.data ? (
              <DataTable
                columns={[
                  { key: 'role', header: '角色', render: (row) => label(ROLES, row.role) },
                  { key: 'healthy', header: '心跳', width: '90px' },
                  {
                    key: 'updated_at',
                    header: '最近心跳',
                    render: (row) => <span className="qp-num">{formatAge(row.updated_at)}</span>,
                  },
                ]}
                rows={status.data.services as unknown as Record<string, unknown>[]}
                rowKey={(row) => String(row['worker'])}
                empty="没有任何心跳记录——Qlib 服务可能都没起来。"
              />
            ) : (
              <LoadingBlock />
            )}
          </Card>

          <Card title="运行设置" meta="只读；改动走部署侧的环境变量">
            {settings.data ? (
              <Facts
                items={[
                  { label: '行情源', value: settings.data.provider },
                  { label: 'Qlib 已启用', value: settings.data.qlib_enabled ? '是' : '否' },
                  { label: '历史采集交易日', value: formatInteger(settings.data.history_sessions) },
                  { label: '行情刷新间隔', value: `${formatInteger(settings.data.quote_seconds)} 秒` },
                  { label: '实时请求上限', value: `${formatInteger(settings.data.realtime_rpm)} 次/分` },
                  { label: '普通请求上限', value: `${formatInteger(settings.data.ordinary_rpm)} 次/分` },
                  { label: '每日配额', value: formatInteger(settings.data.daily_quota) },
                  { label: '配置版本', value: String(settings.data.revision) },
                ]}
              />
            ) : (
              <LoadingBlock />
            )}
          </Card>
        </div>
      </section>

      <section className="qp-section">
        <h2 style={{ marginBottom: 'var(--space-3)' }}>选股策略</h2>
        <Card
          title={`策略版本 ${policy.data?.revision ?? '—'}`}
          meta="策略变更会使既有报告失效，并要求匹配一个已合格的发布版本"
        >
          {policy.data ? (
            <Facts
              items={[
                { label: '价格上限', value: `¥${formatNumber(policy.data.data.price_ceiling)}` },
                { label: '最小上市交易日', value: formatInteger(policy.data.data.minimum_sessions) },
                { label: '最小成交额', value: `¥${formatInteger(policy.data.data.minimum_turnover)}` },
                { label: '取前 N 只', value: formatInteger(policy.data.data.top_n) },
                { label: '单票上限', value: `${formatNumber(policy.data.data.max_weight * 100, 1)}%` },
                { label: '现金下限', value: `${formatNumber(policy.data.data.minimum_cash * 100, 1)}%` },
                { label: '变动带', value: `${formatNumber(policy.data.data.change_band * 100, 2)}%` },
                { label: '最小覆盖率', value: `${formatNumber(policy.data.data.minimum_coverage * 100, 0)}%` },
              ]}
            />
          ) : (
            <LoadingBlock />
          )}
        </Card>
      </section>

      <section className="qp-section">
        <Card title="依赖关系" meta="这一屏对应的是「今日」判决面背后的链路">
          <ol className="qp-caption" style={{ margin: 0, paddingLeft: '1.2em', lineHeight: 2 }}>
            <li>数据源验证 —— 每个必需接口都要有一次成功的真实采集</li>
            <li>采集范围 —— 生产验收要求全市场日线覆盖率 ≥95%</li>
            <li>Qlib 引擎 —— 三个工作进程的心跳</li>
            <li>数据代次 —— 通过验证的不可变导出</li>
            <li>模型发布 —— 已评估通过 + 已批准 + 未过期</li>
            <li>今日建议 —— 一份满足全部有效条件的建议</li>
          </ol>
          <p className="qp-caption" style={{ marginTop: 'var(--space-3)' }}>
            前三条互相独立，可以同时处理；从第四条起顺序依赖。
          </p>
          <div className="qp-row" style={{ marginTop: 'var(--space-3)' }}>
            <Badge tone="idle" plain>
              引擎版本 {status.data ? String((status.data.metrics as Record<string, unknown>)['engine'] ?? '—') : '—'}
            </Badge>
            <Badge tone="idle" plain>
              频率 {Object.values(FREQUENCIES).join(' / ')}
            </Badge>
            <Badge tone="idle" plain>
              用途 {Object.values(PURPOSES).join(' / ')}
            </Badge>
          </div>
        </Card>
      </section>

      <p className="qp-caption" style={{ marginTop: 'var(--space-5)' }}>
        代次最新一条创建于 {formatDay(generations.data?.[0]?.created_at)}。
      </p>
    </>
  );
}

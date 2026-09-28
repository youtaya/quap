/**
 * 模型与验证。职责：**在人工发布前查看评估与影子证据**。
 *
 * 发布是人工决定，所以这一屏的重点是把「凭什么发布」摊开：评估结果、影子观察、诊断指标。
 * 训练入口保留在这里，但判决面也能发起——因为「没有可用模型」是一条卡点，卡点必须就地可解。
 */

import { useState } from 'react';

import { api } from '@/api/client';
import type { ModelRecord, Page, ShadowSample } from '@/api/types';
import { useApi } from '@/api/useApi';
import { DataTable } from '@/components/DataTable';
import { Badge, Banner, Button, Card, EmptyState, Facts, LoadingBlock } from '@/components/ui';
import { useToast } from '@/components/toast';
import { formatDay, formatMoment, formatNumber, summarize } from '@/domain/format';
import { FREQUENCIES, STATES, label } from '@/domain/labels';

interface FactorDefinition extends Record<string, unknown> {
  id: string;
  name: string;
  revision: number;
  created_at: string;
}

export function Models() {
  const models = useApi<ModelRecord[]>('/models');
  const inventory = useApi<{ items: { id: string; frequency?: string; feature_count?: number }[] }>(
    '/factors/inventory',
  );
  const factors = useApi<Page<FactorDefinition>>('/factors?limit=50');
  const experiments = useApi<Page<Record<string, unknown>>>('/research-experiments?limit=25');
  const toast = useToast();
  const [frequency, setFrequency] = useState<'day' | '5min'>('day');
  const [busy, setBusy] = useState<string | null>(null);
  const [shadowOf, setShadowOf] = useState<ModelRecord | null>(null);
  const shadow = useApi<ShadowSample[]>(shadowOf ? `/models/${shadowOf.id}/shadow-observations` : null);

  async function train() {
    setBusy('train');
    try {
      const result = await api.post<{ run_id: string }>('/model-training-runs', {
        purpose: 'training',
        frequency,
        request_key: crypto.randomUUID(),
      });
      toast.push('ok', '已提交训练', `任务 ${result.run_id} 已排队。进度见「运行记录与配置」。`);
      models.refresh();
    } catch (failure) {
      toast.push('error', '提交训练失败', failure instanceof Error ? failure.message : '未知错误');
    } finally {
      setBusy(null);
    }
  }

  async function promote(model: ModelRecord) {
    setBusy(model.id);
    try {
      await api.post(`/models/${model.id}/promote`, { expected_active_id: activeId(models.data) });
      toast.push('ok', '已发布', `模型 ${model.id.slice(0, 8)} 现在是指定频率的活跃版本。`);
      models.refresh();
    } catch (failure) {
      toast.push('error', '发布失败', failure instanceof Error ? failure.message : '未知错误');
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      <div className="qp-heading">
        <h1>模型与验证</h1>
        <p>在人工发布前查看评估与影子证据。判决只认「已评估通过 + 已批准 + 未过期」的模型。</p>
      </div>

      <Banner
        tone="info"
        title="发布是人工决定，平台不会自动发布"
        detail="候选模型必须先在开发折上通过评估，再由人确认发布。影子观察中的模型不参与判决，也不会出现在「今日」的建议里。"
      />

      <section className="qp-section">
        <Card
          title="训练候选模型"
          meta="训练只是第一步；通过评估后还需要人工发布"
          actions={
            <div className="qp-row" role="radiogroup" aria-label="训练频率">
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
          <div className="qp-row">
            <Button variant="primary" disabled={busy !== null} onClick={train}>
              {busy === 'train' ? '已提交…' : `训练${FREQUENCIES[frequency]}候选模型`}
            </Button>
            <span className="qp-caption">
              训练读取最新的不可变数据代次；没有可用代次时会先准备一份。
            </span>
          </div>
        </Card>
      </section>

      <section className="qp-section">
        <h2 style={{ marginBottom: 'var(--space-3)' }}>模型版本</h2>
        {models.loading ? (
          <LoadingBlock />
        ) : models.error ? (
          <Banner tone="error" title="读不到模型列表" detail={models.error.detail} />
        ) : (
          <DataTable
            columns={[
              { key: 'id', header: '模型', render: (row) => String(row['id']).slice(0, 8) },
              { key: 'frequency', header: '频率', width: '80px' },
              {
                key: 'state',
                header: '状态',
                width: '100px',
                render: (row) => (
                  <Badge tone={row['state'] === 'active' ? 'ok' : row['state'] === 'shadow' ? 'busy' : 'idle'} plain>
                    {label(STATES, row['state'])}
                  </Badge>
                ),
              },
              {
                key: 'passed',
                header: '评估',
                width: '90px',
                render: (row) =>
                  row['passed'] ? (
                    <Badge tone="ok" plain>
                      通过
                    </Badge>
                  ) : (
                    <Badge tone="error" plain>
                      未通过
                    </Badge>
                  ),
              },
              { key: 'created_at', header: '训练时刻' },
              { key: 'expires_at', header: '过期时刻' },
              {
                key: 'evaluation',
                header: '评估摘要',
                render: (row) => <span className="qp-caption">{summarize(row['evaluation'])}</span>,
              },
              {
                key: '_actions',
                header: '操作',
                render: (row) => (
                  <div className="qp-row" style={{ gap: 'var(--space-1)' }}>
                    <Button
                      size="sm"
                      disabled={busy !== null || row['state'] === 'active'}
                      onClick={() => promote(row as unknown as ModelRecord)}
                    >
                      {row['state'] === 'active' ? '已发布' : '发布'}
                    </Button>
                    <Button size="sm" variant="quiet" onClick={() => setShadowOf(row as unknown as ModelRecord)}>
                      影子观察
                    </Button>
                  </div>
                ),
              },
            ]}
            rows={models.data as unknown as Record<string, unknown>[]}
            rowKey={(row) => String(row['id'])}
            empty="还没有任何模型版本。先训练一个候选模型。"
            caption="模型版本与评估"
          />
        )}
      </section>

      {shadowOf ? (
        <section className="qp-section">
          <Card
            title={`影子观察 · ${shadowOf.id.slice(0, 8)}`}
            meta="候选模型在最新实盘截点上的观察记录；它不参与判决"
            actions={
              <Button size="sm" variant="quiet" onClick={() => setShadowOf(null)}>
                收起
              </Button>
            }
          >
            {shadow.loading ? (
              <LoadingBlock />
            ) : (
              <DataTable
                columns={[
                  { key: 'day', header: '交易日' },
                  {
                    key: 'data',
                    header: '观察结果',
                    render: (row) => <span className="qp-caption">{summarize(row['data'])}</span>,
                  },
                ]}
                rows={shadow.data as unknown as Record<string, unknown>[]}
                rowKey={(row, index) => `${String(row['day'])}-${index}`}
                empty="这个模型还没有影子观察记录。"
                caption="影子观察记录"
              />
            )}
          </Card>
        </section>
      ) : null}

      <section className="qp-section">
        <h2 style={{ marginBottom: 'var(--space-3)' }}>因子库</h2>
        <div className="qp-grid">
          <Card title="内置因子" meta="来自 experiments.inventory()，只读">
            {inventory.loading ? (
              <LoadingBlock />
            ) : inventory.data && inventory.data.items.length > 0 ? (
              <div className="qp-row">
                {/* 键是 `id`：`/factors/inventory` 返回的是 `{id, frequency, feature_count, …}`，
                    没有 `name` 字段。用 `name` 当键会让 React 收到 undefined 并整条列表失去身份。 */}
                {inventory.data.items.map((item) => (
                  <Badge key={item.id} tone="idle" plain>
                    {item.id}
                    {item.feature_count ? ` · ${item.feature_count} 个特征` : ''}
                  </Badge>
                ))}
              </div>
            ) : (
              <EmptyState>没有内置因子。</EmptyState>
            )}
          </Card>

          <Card title="实验性因子定义" meta="实验性定义不能被采纳为目标权重">
            <DataTable
              columns={[
                { key: 'name', header: '因子名称' },
                { key: 'revision', header: '版本', numeric: true },
                { key: 'created_at', header: '创建时刻' },
              ]}
              rows={(factors.data?.items ?? []) as unknown as Record<string, unknown>[]}
              rowKey={(row) => String(row['id'])}
              empty="还没有实验性因子定义。"
              caption="实验性因子定义"
            />
          </Card>
        </div>
      </section>

      <section className="qp-section">
        <h2 style={{ marginBottom: 'var(--space-3)' }}>研究实验</h2>
        <DataTable
          columns={[
            { key: 'id', header: '实验', render: (row) => String(row['id']).slice(0, 8) },
            { key: 'name', header: '名称' },
            { key: 'status', header: '状态', width: '100px' },
            { key: 'created_at', header: '创建时刻' },
            {
              key: 'frozen_configuration_id',
              header: '已冻结配置',
              render: (row) => (row['frozen_configuration_id'] ? '已冻结' : '—'),
            },
          ]}
          rows={(experiments.data?.items ?? []) as unknown as Record<string, unknown>[]}
          rowKey={(row) => String(row['id'])}
          empty="还没有研究实验。实验产物不能直接进入生产判决。"
          caption="研究实验"
        />
      </section>

      <section className="qp-section">
        <Card title="发布口径" meta="这一屏的判定标准">
          <Facts
            items={[
              { label: '日线模型有效期', value: '训练日 + 35 天' },
              { label: '五分钟模型有效期', value: '训练日 + 14 天' },
              { label: '发布前置', value: '评估通过 + 人工确认' },
              { label: '当前活跃版本', value: activeId(models.data) ?? '无' },
            ]}
          />
          <p className="qp-caption" style={{ marginTop: 'var(--space-3)' }}>
            过期时间按「训练数据截点」与「创建时刻」中较早者计算，避免用旧数据训出的模型长期占位。
          </p>
        </Card>
      </section>

      <p className="qp-caption" style={{ marginTop: 'var(--space-5)' }}>
        列表读取于 {formatMoment(new Date().toISOString())}；最新模型创建于{' '}
        {formatDay(models.data?.[0]?.created_at)}，共 {formatNumber(models.data?.length ?? 0, 0)} 个版本。
      </p>
    </>
  );
}

function activeId(models: ModelRecord[] | null): string | null {
  return models?.find((model) => model.state === 'active')?.id ?? null;
}

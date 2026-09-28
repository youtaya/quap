/**
 * 门禁卡 —— 判决面的最小单元，也是动作类型三分唯一落地的地方。
 *
 * 三种动作类型的呈现差异是**硬约束**（附录 A.4）：
 *   auto   可自解：主按钮，点了就推进；
 *   manual 半自解：写清「要改什么」；如果平台内确有一步能推进它（例如训练），给次按钮，但不声称
 *                  能直接解开卡点；
 *   ops    需运维：可复制指令 + 明确写出「不做修复按钮」。给一个点了不起作用的按钮，比不给按钮
 *                  更伤信任——这正是现有实现最伤人的一处（`Update source data` 对引擎离线毫无作用）。
 *
 * 三种状态的呈现差异同样是硬约束：
 *   blocked 显示自身判决句 + 动作；
 *   waiting **不显示自身失败状态**，只说在等谁（附录 A.3 规则 2）；
 *   passed  显示一句「已通过」，判决面不该只报坏消息。
 */

import { useState } from 'react';

import { Badge, Button, CopyableCommand } from './ui';
import { CAPABILITY_LABELS, EVIDENCE_LABELS, ROLES, label } from '@/domain/labels';
import type { GateAction, GateView } from '@/domain/verdict';

/**
 * 证据值的渲染。
 *
 * 两个细节值得说明：
 *   1. `missing` / `required` / `verified` 里装的是**能力键**（`history`、`securities`…），
 *      直接显示等于把内部标识符丢给操作员，所以过一遍 `CAPABILITY_LABELS`。
 *   2. `required_roles` / `healthy_roles` 里装的是工作进程角色，同理过 `ROLES`。
 */
const KEY_ARRAYS: Record<string, Record<string, string>> = {
  missing: CAPABILITY_LABELS,
  required: CAPABILITY_LABELS,
  verified: CAPABILITY_LABELS,
  required_roles: ROLES,
  healthy_roles: ROLES,
};

function renderEvidence(value: unknown, key?: string): string {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (Array.isArray(value)) {
    const dictionary = key ? KEY_ARRAYS[key] : undefined;
    return value.length === 0
      ? '（空）'
      : value.map((item) => (dictionary ? label(dictionary, item) : String(item))).join('、');
  }
  return String(value);
}

interface GateCardProps {
  view: GateView;
  /** 上游未通过的门禁显示名，用于把「等待上游」说清楚。 */
  upstreamLabels: string[];
  onRunAction: (action: GateAction) => void;
  actionPending: boolean;
  onRecheck: () => void;
  rechecking: boolean;
}

export function GateCard({
  view,
  upstreamLabels,
  onRunAction,
  actionPending,
  onRecheck,
  rechecking,
}: GateCardProps) {
  const { gate, label, verdict, passedNote, action, tone } = view;
  const [expanded, setExpanded] = useState(false);
  const marker = gate.status === 'passed' ? '✓' : gate.status === 'blocked' ? '✕' : '·';

  const statusText =
    gate.status === 'passed' ? '已通过' : gate.status === 'blocked' ? '卡在这里' : '等待上游';

  return (
    <article className={`qp-gate qp-gate--${gate.status}`} aria-label={`${label}：${statusText}`}>
      <div className="qp-gate__marker" aria-hidden="true">
        {marker}
      </div>

      <div className="qp-gate__body">
        <div className="qp-gate__head">
          <span className="qp-gate__label">{label}</span>
          <Badge tone={tone} plain>
            {statusText}
          </Badge>
          {gate.status === 'blocked' ? (
            <Badge tone="idle" plain>
              {action.kind === 'auto' ? '可自解' : action.kind === 'manual' ? '半自解' : '需运维'}
            </Badge>
          ) : null}
        </div>

        {verdict ? (
          <p className="qp-gate__text">
            {/* 判决句由 `domain/verdict.ts` 按门禁 key 组稿，数字来自后端 evidence。 */}
            {verdict}
          </p>
        ) : null}

        {passedNote ? <p className="qp-gate__text qp-muted">{passedNote}</p> : null}

        {gate.status === 'waiting' ? (
          <p className="qp-gate__waiting">
            等待上游：
            {upstreamLabels.length > 0 ? (
              <>
                <b>{upstreamLabels.join('、')}</b> 尚未通过，所以这一环的真实状态还无从判断。
              </>
            ) : (
              '上游环节尚未通过。'
            )}{' '}
            先处理上面标红的那一项。
          </p>
        ) : null}

        {gate.status === 'blocked' ? (
          <div className="qp-gate__action">
            {action.kind === 'auto' ? (
              <>
                <div className="qp-gate__buttons">
                  <Button variant="primary" onClick={() => onRunAction(action)} disabled={actionPending}>
                    {actionPending ? '已提交，等待后端…' : action.label}
                  </Button>
                  <Button variant="quiet" onClick={onRecheck} disabled={rechecking}>
                    {rechecking ? '正在重新检测…' : '我已处理，重新检测'}
                  </Button>
                </div>
                {action.hint ? <p className="qp-gate__hint">{action.hint}</p> : null}
              </>
            ) : null}

            {action.kind === 'manual' ? (
              <>
                <p className="qp-gate__hint">{action.hint}</p>
                {action.command ? <CopyableCommand command={action.command} /> : null}
                <div className="qp-gate__buttons">
                  {action.request ? (
                    <Button onClick={() => onRunAction(action)} disabled={actionPending}>
                      {actionPending ? '已提交，等待后端…' : action.label}
                    </Button>
                  ) : null}
                  <Button variant="quiet" onClick={onRecheck} disabled={rechecking}>
                    {rechecking ? '正在重新检测…' : '我已处理，重新检测'}
                  </Button>
                </div>
              </>
            ) : null}

            {action.kind === 'ops' ? (
              <>
                <p className="qp-gate__nofix">
                  这一步在平台进程之外，所以本页<b>不做修复按钮</b>。按下面这条指令处理，然后点「我已处理，
                  重新检测」——平台不会自动轮询，免得你以为它自己恢复了。
                </p>
                {action.command ? <CopyableCommand command={action.command} /> : null}
                <div className="qp-gate__buttons">
                  <Button onClick={onRecheck} disabled={rechecking}>
                    {rechecking ? '正在重新检测…' : '我已处理，重新检测'}
                  </Button>
                </div>
              </>
            ) : null}
          </div>
        ) : null}

        {/* L3：原始证据默认折叠。判决句负责让人读懂，这里负责让人**核对**。 */}
        {Object.keys(gate.evidence).length > 0 ? (
          <details className="qp-evidence" onToggle={(event) => setExpanded(event.currentTarget.open)}>
            <summary>{expanded ? '收起判定证据' : '查看判定证据'}</summary>
            <div className="qp-evidence__body">
              <p className="qp-caption" style={{ marginBottom: 'var(--space-2)' }}>
                以下字段直接来自接口的 <code>evidence</code>，判决句里的数字就取自这里。
              </p>
              <dl
                style={{
                  display: 'grid',
                  gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))',
                  gap: 'var(--space-3)',
                  margin: 0,
                  fontSize: '0.75rem',
                }}
              >
                {Object.entries(gate.evidence).map(([key, value]) => (
                  <div key={key} style={{ minWidth: 0 }}>
                    <dt className="qp-caption">
                      {EVIDENCE_LABELS[key] ?? key}{' '}
                      <code style={{ opacity: 0.7, fontSize: '0.6875rem' }}>{key}</code>
                    </dt>
                    <dd className="qp-num" style={{ margin: 0, overflowWrap: 'anywhere' }}>
                      {renderEvidence(value, key)}
                    </dd>
                  </div>
                ))}
              </dl>
            </div>
          </details>
        ) : null}
      </div>
    </article>
  );
}

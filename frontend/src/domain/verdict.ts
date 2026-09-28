/**
 * 判决句组稿层 —— 这个文件就是 P2 的实质。
 *
 * 它做一件现有实现没做的事：**把 `blockers` 里的字符串当作机器可读原因，而不是界面文案**。
 * 后端给结构（`gates[]`，每条带 `key` / `status` / `action` / `evidence`），这里按 `key` 组稿成
 * 操作员能读的判决句，数字全部取自 `evidence`。
 *
 * 为什么不把判决句写在后端：文案是展示层的事，事实是服务端的事。后端只要保证 `evidence` 里的
 * 数字是对的，前端改一句话不需要动 API；反过来新增一条门禁，后端加一条 `GATE_SPECS` 即可，
 * 前端在 `GATE_COPY` 里补一条文案——两边都不需要去猜对方。
 *
 * 设计依据：`docs/DESIGN.md` 附录 A.3（依赖链）、A.4（动作类型三分）、A.5（判决句映射）。
 */

import type { FrequencyReadiness, Gate, GateEvidence, GateStage, Readiness } from '@/api/types';

/** 六种状态，与 `styles/tokens.css` 的 `--state-*` 一一对应（附录 B）。 */
export type Tone = 'ok' | 'warn' | 'error' | 'info' | 'busy' | 'idle';

/** 判决面上可以发起的动作。没有 `request` 就是平台内做不了，此时**不画按钮**。 */
export interface GateAction {
  kind: 'auto' | 'manual' | 'ops';
  label: string;
  /** 按钮下方的一行小字：预计耗时、或者「要改什么」。 */
  hint?: string;
  /** 需运维类的可复制指令。 */
  command?: string;
  /** 平台内的真实请求；`ops` 类永远没有它——这是 A.4 的硬约束。 */
  request?: { method: 'POST' | 'PUT'; path: string; body?: unknown };
}

interface GateContext {
  frequency: 'day' | '5min';
  frequencyLabel: string;
  /** 「日线接口」/「日线与分钟线接口」——能力门禁的计数主语。 */
  interfaceNoun: string;
}

interface GateCopy {
  label: string;
  /** 卡住时的「因为」。只在**上游已通过**时才是判决句；否则整条显示「等待上游」。 */
  reason: (evidence: GateEvidence, context: GateContext) => string;
  /** 通过时的一句话。判决面不该只报坏消息。 */
  passed: (evidence: GateEvidence, context: GateContext) => string;
  action: (context: GateContext) => GateAction;
}

const collect = { method: 'POST' as const, path: '/control', body: { action: 'refresh' } };
const recheckHint = '处理完点「我已处理，重新检测」——平台不自动轮询，免得你以为它自己恢复了。';

export const GATE_COPY: Record<string, GateCopy> = {
  capability: {
    label: '数据源验证',
    reason: (e, c) =>
      `${e.required_count ?? 0} 个${c.interfaceNoun}里有 ${e.missing_count ?? 0} 个从未成功完成一次真实采集` +
      `${e.missing_labels?.length ? `（${e.missing_labels.join('、')}）` : ''}。` +
      '平台不承认没有采集证据的数据，所以也就不会拿它训练或推理。',
    passed: (e, c) => `${e.required_count ?? 0} 个${c.interfaceNoun}全部完成过真实采集。`,
    action: () => ({
      kind: 'auto',
      label: '立即采集',
      // 不再重复「完成后要手动重新检测」——旁边的按钮就叫这个名字，说两遍是噪音。
      hint: '后台执行，离开页面不影响任务。',
      request: collect,
    }),
  },

  scope: {
    label: '采集范围',
    reason: (e) =>
      `当前采集范围是「${e.scope ?? '未知'}」，只覆盖部分市场。` +
      `生产验收要求全市场日线覆盖率至少 ${Math.round((e.minimum_coverage ?? 0.95) * 100)}%。`,
    passed: () => '采集范围是全市场。',
    action: () => ({
      kind: 'manual',
      label: '修改采集范围',
      hint: `把环境变量 QUANT_HISTORY_SCOPE 设为 all，然后重建并重启采集相关服务。${recheckHint}`,
      command: 'QUANT_HISTORY_SCOPE=all docker compose -f deploy/compose.yaml up -d --build history minute-history quotes',
    }),
  },

  engine: {
    label: 'Qlib 引擎',
    reason: () =>
      'Qlib 的工作进程没有心跳，训练与推理都无法执行。' +
      '这一步在平台里点不出来——需要有人到部署机上把服务启动起来。',
    passed: () => 'Qlib 服务心跳正常。',
    action: () => ({
      // 需运维类：给指令 + 复检，**不给修复按钮**。给一个点了不起作用的按钮，比不给按钮更伤信任。
      kind: 'ops',
      label: '启动 Qlib 服务',
      hint: `这一步在平台进程之外，所以本页不做修复按钮。${recheckHint}`,
      command: 'docker compose -f deploy/compose.yaml up -d qlib-data qlib-train qlib-daily qlib-intraday',
    }),
  },

  generation: {
    label: '数据代次',
    reason: (_e, c) => `今天还没有一份通过验证的${c.frequencyLabel}数据代次，模型没有可训练的数据。`,
    passed: (_e, c) => `已有通过验证的${c.frequencyLabel}数据代次。`,
    action: (c) => ({
      kind: 'auto',
      label: '运行数据流水线',
      hint: '先准备不可变数据代次，再推理。约 3–10 分钟，后台执行。',
      request: {
        method: 'POST',
        path: '/pipeline-runs',
        body: { purpose: 'inference', frequency: c.frequency, request_key: '' },
      },
    }),
  },

  model: {
    label: '模型发布',
    reason: () =>
      '没有一份「已评估通过 + 已批准 + 未过期」的模型。' +
      '训练可以在这里发起，但发布是人工决定：候选模型必须先通过评估，再由人确认。',
    passed: () => '已有已发布、已合格且未过期的模型。',
    action: (c) => ({
      // 半自解：训练能推进它，但推不到底——发布这一步必须有人看评估。所以主文案是「要做什么」，
      // 训练只是其中的第一步，不声称能直接解开卡点。
      kind: 'manual',
      label: '训练候选模型',
      hint: `训练完成后到「模型与验证」查看评估，通过后再发布。${recheckHint}`,
      request: {
        method: 'POST',
        path: '/model-training-runs',
        body: { purpose: 'training', frequency: c.frequency, request_key: '' },
      },
    }),
  },

  day_baseline: {
    label: '日线模型基线',
    reason: () =>
      '五分钟频率以日线模型作为基线，而日线模型还没有发布。' +
      '这条卡点属于日线链路，在五分钟这里无法推进。',
    passed: () => '日线基线模型已发布。',
    action: () => ({
      kind: 'ops',
      label: '到日线链路处理',
      hint: '按「今日」页日线链路上的卡点逐步处理，日线模型发布后这条会自动解开。',
    }),
  },

  // ⑥ 产出。它不是「就绪条件」而是「产出状态」，数据来自 `readiness.frequencies[f].output`
  // 而不是 `gates`（后端不把它算进 `ready`：能不能跑与跑没跑出来是两个问题）。
  recommendation: {
    label: '今日建议',
    reason: () => '前置条件都已齐备，但今天还没有一份有效的建议。缺的是把推理跑出来。',
    passed: (_e, c) => `今天已有一份有效的${c.frequencyLabel}建议。`,
    action: (c) => ({
      kind: 'auto',
      label: '立即推理',
      hint: '复用今天已准备的数据代次，不会重新采集。',
      request: {
        method: 'POST',
        path: '/pipeline-runs',
        body: { purpose: 'inference', frequency: c.frequency, request_key: '' },
      },
    }),
  },
};

/** 门禁 key 没有文案时的兜底。**故意显示 key**，让缺失一眼可见，而不是静默显示空字符串。 */
const UNKNOWN_COPY: GateCopy = {
  label: '未登记的门禁',
  reason: () => '这条门禁在前端没有登记文案。请补齐 domain/verdict.ts 的 GATE_COPY。',
  passed: () => '已通过。',
  action: () => ({ kind: 'ops', label: '无可用动作', hint: '这条门禁尚未在前端登记。' }),
};

export function copyFor(key: string): GateCopy {
  return GATE_COPY[key] ?? UNKNOWN_COPY;
}

/**
 * 判决面的最低契约：`gates` 必须存在且是数组。
 *
 * 加这道检查是因为它真的发生过：把新前端指向一个还没重建的旧 API 时，`gates` 不存在，
 * 判决面直接白屏——而白屏对操作员来说是最糟的失败形态，它连「哪里不对」都不说。
 * 版本错配要**说清楚**，而不是崩掉。
 */
export function gateChainOf(readiness: Readiness, frequency: 'day' | '5min'): Gate[] | null {
  // 这里的类型断言是**有意的**：`Readiness` 描述的是当前后端应有的形状，而本函数存在的理由
  // 正是「后端可能不是这个形状」。把入参当不可信数据处理，才不会自欺欺人。
  const frequencies = readiness.frequencies as
    | Partial<Record<'day' | '5min', FrequencyReadiness>>
    | undefined;
  const state = frequencies?.[frequency];
  return Array.isArray(state?.gates) ? state.gates : null;
}

export function contextFor(frequency: 'day' | '5min'): GateContext {
  return frequency === 'day'
    ? { frequency: 'day', frequencyLabel: '日线', interfaceNoun: '日线接口' }
    : { frequency: '5min', frequencyLabel: '五分钟', interfaceNoun: '日线与分钟线接口' };
}

/* ── 门禁的呈现视图模型 ─────────────────────────────────────────────────── */

export interface GateView {
  gate: Gate;
  label: string;
  /** 卡住时的判决句；`waiting` 与 `passed` 时为 null。 */
  verdict: string | null;
  /** 通过时的说明；仅 `passed` 时有值。 */
  passedNote: string | null;
  action: GateAction;
  tone: Tone;
}

export function gateView(gate: Gate, frequency: 'day' | '5min'): GateView {
  const copy = copyFor(gate.key);
  const context = contextFor(frequency);
  const tone: Tone =
    gate.status === 'passed' ? 'ok' : gate.status === 'blocked' ? 'error' : 'idle';
  return {
    gate,
    label: copy.label,
    verdict: gate.status === 'blocked' ? copy.reason(gate.evidence, context) : null,
    passedNote: gate.status === 'passed' ? copy.passed(gate.evidence, context) : null,
    action: copy.action(context),
    tone,
  };
}

/**
 * ⑥ 产出的视图模型。链路必须在「今天有没有出建议」处收口——否则操作员看完整条链路，
 * 仍然不知道结论。
 *
 * 它的状态由两件事合成：`ready`（上游全通）与 `output.published`（建议在不在）。
 * 上游没通时显示「等待上游」，这与阶段二的规则一致。
 */
export function outputGateView(readiness: Readiness, frequency: 'day' | '5min'): GateView {
  const state = readiness.frequencies[frequency];
  const status: Gate['status'] = !state.ready ? 'waiting' : state.output.published ? 'passed' : 'blocked';
  const gate: Gate = {
    key: 'recommendation',
    stage: 'output',
    stage_label: '产出',
    action: 'auto',
    depends_on: ['model'],
    status,
    evidence: {
      published: state.output.published,
      valid_until: state.output.valid_until ?? '',
      recommendation_id: state.output.recommendation_id ?? '',
    },
  };
  return gateView(gate, frequency);
}

export interface StageGroup {
  stage: GateStage;
  label: string;
  /**
   * 阶段的依赖性质，由 `depends_on` 推导而不是写死。附录 A.3 规则 4 要求阶段标签必须声明它：
   * 标成「顺序依赖」而实际互相独立，操作员会去等一个根本不需要等的前置。
   */
  dependency: 'independent' | 'sequential';
  gates: GateView[];
}

export function groupByStage(gates: Gate[], frequency: 'day' | '5min'): StageGroup[] {
  const order: GateStage[] = ['foundation', 'data-model', 'output'];
  const views = gates.map((gate) => gateView(gate, frequency));
  return order
    .map((stage) => {
      const inStage = views.filter((view) => view.gate.stage === stage);
      const keys = new Set(inStage.map((view) => view.gate.key));
      const sequential = inStage.some((view) => view.gate.depends_on.some((key) => keys.has(key)));
      return {
        stage,
        label: inStage[0]?.gate.stage_label ?? stage,
        dependency: sequential ? ('sequential' as const) : ('independent' as const),
        gates: inStage,
      };
    })
    .filter((group) => group.gates.length > 0);
}

/**
 * 完整的解锁链路：阶段一、阶段二来自 `gates`，阶段三由 `output` 合成。
 * 阶段三的依赖性质固定为 `sequential`——它依赖 `model`。
 */
export function fullChain(readiness: Readiness, frequency: 'day' | '5min'): StageGroup[] {
  const groups = groupByStage(readiness.frequencies[frequency].gates, frequency);
  groups.push({
    stage: 'output',
    label: '产出',
    dependency: 'sequential',
    gates: [outputGateView(readiness, frequency)],
  });
  return groups;
}

/* ── 判决条 ─────────────────────────────────────────────────────────────── */

export interface Verdict {
  tone: Tone;
  /** 抬头：哪条频率的判决。判决条上「今日判决」下方那一行小字。 */
  eyebrow: string;
  /** 一句话结论，**不含**频率前缀——频率由 `eyebrow` 承载。 */
  headline: string;
  detail: string;
  /** 操作员此刻该做的那一件事。`null` 表示没有可发起的动作（例如全部就绪）。 */
  action: GateAction | null;
  /** 阶段一的卡点：**可能不止一条，全部展示**（A.3 规则 1）。 */
  independent: GateView[];
  /** 阶段二起的第一个卡点；其余下游显示「等待上游」（A.3 规则 2）。 */
  sequential: GateView | null;
  /** 该频率是否整体就绪。 */
  ready: boolean;
  /** ⑥ 是否已有有效建议。 */
  published: boolean;
  reportId: string | null;
  validUntil: string | null;
}

export function composeVerdict(
  readiness: Readiness,
  frequency: 'day' | '5min',
): Verdict {
  const state = readiness.frequencies[frequency];
  const context = contextFor(frequency);
  const views = state.gates.map((gate) => gateView(gate, frequency));
  const blocked = views.filter((view) => view.gate.status === 'blocked');

  // 阶段一三项互相独立，可以同时是卡点；阶段二起只取第一个（其余 status 已是 `waiting`）。
  const independent = blocked.filter((view) => view.gate.stage === 'foundation');
  const sequential = blocked.find((view) => view.gate.stage !== 'foundation') ?? null;

  const base = {
    eyebrow: `今日判决 · ${context.frequencyLabel}`,
    independent,
    sequential,
    ready: state.ready,
    published: state.output.published,
    reportId: state.output.recommendation_id,
    validUntil: state.output.valid_until,
  };

  if (!state.ready) {
    const lead = independent[0] ?? sequential;
    const count = independent.length + (sequential ? 1 : 0);
    return {
      ...base,
      tone: 'error',
      headline: '今天不能出建议',
      detail: lead
        ? `${lead.label}没有通过。${count > 1 ? `当前有 ${count} 个环节卡住，下面按依赖顺序列出。` : '下面是这一环的具体情况。'}`
        : '前置条件没有全部满足。',
      action: lead?.action ?? null,
    };
  }

  if (!state.output.published) {
    return {
      ...base,
      tone: 'info',
      headline: '前置条件已齐备，今天还没出建议',
      detail: '所有门禁都已通过，只差把今天的推理跑出来。',
      action: {
        kind: 'auto',
        label: '立即推理',
        hint: '复用今天已准备的数据代次，不会重新采集。',
        // `request_key` 留空，由提交方在点击那一刻生成：复用同一个键会被后端当作重复请求，
        // 于是「失败后再试一次」会拿回上一次的结果。
        request: {
          method: 'POST',
          path: '/pipeline-runs',
          body: { purpose: 'inference', frequency, request_key: '' },
        },
      },
    };
  }

  return {
    ...base,
    tone: 'ok',
    headline: '今天已出建议',
    detail: state.output.valid_until
      ? `本份建议在 ${state.output.valid_until} 之前有效；过期后需要重新推理。`
      : '本份建议有效。',
    action: null,
  };
}

/**
 * 五分钟叠加的一句话。它**不进入日线判决句**——两条频率的判决必须解耦，否则操作员会以为
 * 日线也被拖住了（A.5）。
 */
export function overlayNote(overlay: FrequencyReadiness): string {
  return overlay.ready
    ? overlay.output.published
      ? '五分钟叠加：已就绪，建议已出'
      : '五分钟叠加：已就绪，尚未出建议'
    : '五分钟叠加：同样受阻 · 不影响日线建议的解锁';
}

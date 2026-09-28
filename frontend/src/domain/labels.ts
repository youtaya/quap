/**
 * 枚举的显示名。**键是路由标识符与后端枚举值，必须保持原样**——只有显示名是中文化。
 *
 * 这条约束来自 P0 的教训：工作空间的 `path` 同时是路由标识符（`Sidebar` 按它建索引，`AppShell`
 * 按它反查当前工作空间，`resolveWorkspace` 也按它做前缀匹配），改键会同时打断导航与测试。
 * 所以中文只落在 `label` 上。
 *
 * 词表来源分两类：`WORKSPACES` / `FREQUENCIES` / `STATES` 原先是
 * `src/quant_platform/dashboard/workflow.py` 的镜像，那个文件已随 Streamlit 版一起删除，这里
 * 就是唯一定义；`CAPABILITY_LABELS` 则仍需与 `src/quant_platform/domain/workflow.py` 保持
 * 一致——它还在后端，`pipeline.py` 用同一份词表拼就绪阻塞文案。
 */

export interface Workspace {
  /** 路由键，保持英文。 */
  key: string;
  /** 侧边栏显示名。 */
  label: string;
  /** 页标题下的一句话：这个空间能回答什么问题。 */
  description: string;
  path: string;
}

export const WORKSPACES: Workspace[] = [
  {
    key: 'Overview',
    label: '今日',
    description: '今天能不能出建议，以及为什么。',
    path: '/today',
  },
  {
    key: 'My Model Portfolio',
    label: '我的组合',
    description: '查看基线权重与现金，采纳版本化的 Qlib 建议。',
    path: '/portfolio',
  },
  {
    key: 'Stock Research',
    label: '个股研究',
    description: '查看行情与模型视角，不代表券商持仓。',
    path: '/stocks',
  },
  {
    key: 'Qlib Report',
    label: '研究报告',
    description: '因子 IC、前瞻收益分位与相对基准的等权组合。',
    path: '/report',
  },
  {
    key: 'Low-Price Scan',
    label: '选股发现',
    description: '用 Qlib 预测给低价股排序，而不是按便宜程度。',
    path: '/discovery',
  },
  {
    key: 'Models & Validation',
    label: '模型与验证',
    description: '在人工发布前查看评估与影子证据。',
    path: '/models',
  },
  {
    key: 'Data & Pipeline',
    label: '运行记录与配置',
    description: '追溯前置条件、不可变代次、依赖关系与定向重试。',
    path: '/pipeline',
  },
];

export function workspaceByPath(pathname: string): Workspace | undefined {
  return WORKSPACES.find((item) => pathname === item.path || pathname.startsWith(item.path + '/'));
}

export const FREQUENCIES: Record<string, string> = { day: '日线', '5min': '五分钟' };

export const STATES: Record<string, string> = {
  pending: '等待执行',
  running: '执行中',
  completed: '已完成',
  complete: '已完成',
  done: '已完成',
  failed: '执行失败',
  blocked: '受阻',
  shadow: '影子观察',
  active: '已发布',
  retired: '已退役',
  succeeded: '已成功',
  rejected: '未通过',
  quarantined: '已隔离',
  available: '可用',
  unavailable: '不可用',
  ready: '已就绪',
  frozen: '已冻结',
  cancelled: '已取消',
  published: '已发布',
};

/** 与 `quant_platform.domain.workflow.CAPABILITY_LABELS` 一致。 */
export const CAPABILITY_LABELS: Record<string, string> = {
  securities: '证券目录',
  calendar: '交易日历',
  history: '历史采集',
  factors: '复权因子',
  benchmark: '基准指数',
  constraints: '涨跌停推导',
  security_history: '风险状态',
  quotes: '行情采集',
  minutes: '分钟线',
};

export const BOARDS: Record<string, string> = {
  'STAR Market': '科创板',
  ChiNext: '创业板',
  'Main Board': '沪深主板',
};

/** 板块卡右上角的角标。键用于匹配 API 的 `board` 字段，所以键保持英文，角标用中文简称。 */
export const BOARD_CODES: Record<string, string> = {
  'STAR Market': '科创',
  ChiNext: '创业',
  'Main Board': '主板',
};

export const KINDS: Record<string, string> = {
  stock: '个股报告',
  basket: '篮子报告',
  screen: '筛选报告',
  factor: '因子报告',
  backtest: '回测报告',
  market: '市场报告',
  qlib: 'Qlib 研究报告',
};

export const QUEUES: Record<string, string> = {
  history: '历史采集',
  quotes: '行情采集',
  analysis: '分析',
  notify: '通知',
  'minute-history': '分钟历史',
  'minute-live': '分钟实时',
  'qlib-data': 'Qlib 数据',
  'qlib-train': 'Qlib 训练',
  'qlib-daily': 'Qlib 日线',
  'qlib-intraday': 'Qlib 五分钟',
  'research-data': '研究数据',
  'qlib-research': 'Qlib 研究',
  'qlib-diagnostics': 'Qlib 诊断',
};

export const ROLES: Record<string, string> = {
  scheduler: '调度器',
  quotes: '行情采集',
  history: '历史采集',
  analysis: '分析',
  operations: '运维',
  notify: '通知',
  'minute-history': '分钟历史',
  'minute-live': '分钟实时',
  'qlib-data': 'Qlib 数据',
  'qlib-train': 'Qlib 训练',
  'qlib-daily': 'Qlib 日线',
  'qlib-intraday': 'Qlib 五分钟',
};

/** 建议里每一行的动作。来自 `domain.workflow.action_for`。 */
export const ACTIONS: Record<string, string> = {
  unavailable: '不可用',
  watch: '观察',
  exit: '清仓',
  hold: '持有',
  add: '建仓',
  increase: '加仓',
  reduce: '减仓',
};

/**
 * 门禁 `evidence` 里的字段名。
 *
 * 判决句负责让人读懂，`evidence` 负责让人**核对**——所以这里给出中文名，同时把 API 原字段名
 * 以小字并排显示。只给中文会让人对不上接口返回，只给英文则等于把实现细节丢给操作员。
 */
export const EVIDENCE_LABELS: Record<string, string> = {
  missing: '缺失的能力',
  missing_labels: '缺失的能力（显示名）',
  required: '必需的能力',
  verified: '已验证的能力',
  required_count: '必需项数',
  missing_count: '缺失项数',
  scope: '当前采集范围',
  required_scope: '要求的采集范围',
  minimum_coverage: '最低覆盖率',
  required_roles: '必需的工作进程',
  healthy_roles: '心跳正常的工作进程',
  healthy: '心跳是否正常',
  frequency: '频率',
  generation_count: '已通过验证的代次数',
  missing_frequency: '缺失的频率',
  published: '是否已出建议',
  valid_until: '有效至',
  recommendation_id: '建议编号',
};

/** 采集控制指令。 */
export const CONTROL_ACTIONS: Record<string, string> = {
  refresh: '立即采集',
  doctor: '检查数据源',
  pause: '暂停采集',
  resume: '恢复采集',
};

export const PURPOSES: Record<string, string> = {
  inference: '推理',
  training: '训练',
  shadow: '影子观察',
};

export const ORIGINS: Record<string, string> = {
  pipeline: '流水线准备',
  scheduled: '定时导出',
};

export const RESEARCH_STATUS: Record<string, string> = {
  running: '执行中',
  succeeded: '已成功',
  failed: '执行失败',
  queued: '排队中',
  cancelled: '已取消',
  frozen: '已冻结',
};

/** 通用兜底：命中不到就原样显示，不吞掉信息。 */
export function label(map: Record<string, string>, value: unknown): string {
  if (value === null || value === undefined || value === '') return '—';
  const key = String(value);
  return map[key] ?? key;
}

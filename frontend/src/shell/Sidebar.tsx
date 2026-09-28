/**
 * 侧边栏。两件事：按操作员的决策分组导航，以及常驻的今日判决块。
 *
 * 分组刻意不是后端的工作空间顺序。后端按「系统模块」排（Overview / Data & Pipeline / …），
 * 那是给实现者看的；操作员关心的是「每天做什么」（日常）／「要深挖时看什么」（研究）／
 * 「出问题时查什么」（系统）。
 */

import { NavLink } from 'react-router-dom';

import { useMiniVerdict } from './readiness';
import { WORKSPACES } from '@/domain/labels';

/**
 * 16×16 线性图标，每条路径独立列出。与原型同一套画法，避免视觉上换了个产品。
 *
 * 两个图标特意换掉了原型里的写法：「模型与验证」原是一个齿轮（读起来像「设置」），
 * 「运行记录与配置」原是三横（读起来像「菜单」）。图标要能被猜对，否则不如不要。
 */
const ICONS: Record<string, string[]> = {
  // 今日：房子
  '/today': ['M2 7l6-4.5L14 7v6.5a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V7z'],
  // 我的组合：柱状图
  '/portfolio': ['M2.5 13.5V7M6.5 13.5V3M10.5 13.5V9M14 13.5V5'],
  // 选股发现：放大镜
  '/discovery': ['M7 11.2a4.2 4.2 0 1 0 0-8.4 4.2 4.2 0 0 0 0 8.4ZM10.2 10.2L14 14'],
  // 个股研究：趋势线
  '/stocks': ['M2 12l3.5-4 3 2.5L14 4', 'M10.5 4H14v3.5'],
  // 研究报告：文档
  '/report': ['M2.5 3.5h11v9h-11z', 'M5 6.5h6', 'M5 9.5h4'],
  // 模型与验证：芯片 + 校验
  '/models': ['M5.5 5.5h5v5h-5z', 'M8 2.5v3M8 10.5v3M2.5 8h3M10.5 8h3', 'M6.4 8.1l1.2 1.2 2.2-2.3'],
  // 运行记录与配置：滑杆（配置）
  '/pipeline': [
    'M2.5 5.5h11',
    'M2.5 10.5h11',
    'M4.6 5.5a1.5 1.5 0 1 0 3 0 1.5 1.5 0 1 0-3 0',
    'M8.4 10.5a1.5 1.5 0 1 0 3 0 1.5 1.5 0 1 0-3 0',
  ],
};

interface Group {
  title: string;
  paths: string[];
}

const GROUPS: Group[] = [
  { title: '日常', paths: ['/today', '/portfolio', '/discovery'] },
  { title: '研究', paths: ['/stocks', '/report', '/models'] },
  { title: '系统', paths: ['/pipeline'] },
];

export function Sidebar({ open, onNavigate }: { open: boolean; onNavigate?: () => void }) {
  const verdict = useMiniVerdict();
  const byPath = new Map(WORKSPACES.map((item) => [item.path, item]));

  return (
    <nav className={`qp-sidebar${open ? ' is-open' : ''}`} aria-label="工作空间">
      <div className="qp-brand">
        <div className="qp-brand__logo" aria-hidden="true">
          Q
        </div>
        <div>
          <div className="qp-brand__name">知衡量化</div>
          <div className="qp-brand__sub">QUAP 研究工作台</div>
        </div>
      </div>

      {/* 常驻判决块。放在导航项之外：导航项的选中底是 --brand-500，红/绿在其上达不到 3:1，
          标记颜色会随选中态失效。判决是全局状态，不是导航项的属性。 */}
      <div className={`qp-verdict-mini qp-verdict-mini--${verdict.tone}`}>
        <div className="qp-verdict-mini__key">今日判决</div>
        <div className="qp-verdict-mini__value">
          <span aria-hidden="true">{verdict.glyph}</span>
          <span>{verdict.text}</span>
        </div>
      </div>

      {GROUPS.map((group) => (
        <div key={group.title}>
          <div className="qp-nav-group">{group.title}</div>
          {group.paths.map((path) => {
            const workspace = byPath.get(path);
            if (!workspace) return null;
            return (
              <NavLink
                key={path}
                to={path}
                className={({ isActive }) => `qp-nav-item${isActive ? ' is-active' : ''}`}
                onClick={onNavigate}
              >
                <svg
                  width="16"
                  height="16"
                  viewBox="0 0 16 16"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden="true"
                  focusable="false"
                >
                  {(ICONS[path] ?? ICONS['/pipeline'] ?? []).map((d) => (
                    <path key={d} d={d} />
                  ))}
                </svg>
                {workspace.label}
              </NavLink>
            );
          })}
        </div>
      ))}

      <div className="qp-sidebar__foot">
        沪深 A 股 · <b>人工决策</b>
        <br />
        红涨绿跌 · 北京时间
        <br />
        本平台不执行交易
      </div>
    </nav>
  );
}

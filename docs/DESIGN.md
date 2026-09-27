---
version: 1.0
name: QUAP-知衡量化-设计系统
description: "QUAP（知衡量化）沪深 A 股研究平台的浅色专业风设计系统。以深海军蓝为品牌锚点，单一靛蓝作为唯一强调色，全部数据单元格强制等宽数字（tnum），红涨绿跌遵循中国市场惯例。结构纪律来自 Linear（四级表面阶梯、发丝边框、稀缺强调色、密集数据表、负字距），金融数据的清晰度与克制来自 Stripe（海军蓝墨色、表格数字、单填充 CTA）。界面文案 100% 中文，技术术语通过内联问号气泡解释，普通用户无需任何量化背景即可上手。"
references:
  structure: linear.app
  finance: stripe
  primary: "#3567d6"

colors:
  # 品牌 — 单一强调色，稀缺使用
  brand-700: "#1d3f7d"
  brand-600: "#2a55c8"
  brand-500: "#3567d6"
  brand-400: "#6b93e6"
  brand-200: "#c7d8f7"
  brand-100: "#e4ecfc"
  brand-50: "#f2f6fd"
  # 深色面 — 侧边栏与反色区块
  navy-950: "#0c182e"
  navy-900: "#122340"
  navy-800: "#1a3055"
  navy-700: "#24406e"
  # 深色面专用文字令牌 — 只允许出现在 navy-* 底上
  nav-100: "#a8bcd9"
  nav-200: "#dce6f5"
  nav-300: "#7b8fb3"
  # 深色面状态点/状态文字 — 只允许出现在 navy-* 底上（侧边栏判决标记）
  nav-ok: "#5cc79a"
  nav-err: "#f0848c"
  on-brand: "#ffffff"
  # 文字 — 三级文字灰（全部通过 WCAG AA），加一级非文字色
  ink-900: "#0e1c33"
  ink-700: "#2b3c57"
  ink-500: "#5b6c86"
  ink-200: "#aab6c8"
  # 表面 — 四级阶梯
  canvas: "#f5f7fb"
  surface-1: "#ffffff"
  surface-2: "#fbfcfe"
  surface-3: "#eef2f8"
  hairline: "#e3e9f3"
  hairline-strong: "#cfd8e6"
  # 行情 — 红涨绿跌（中国市场惯例，不可反转）；色值已按 WCAG AA 校正
  quote-up: "#d32935"
  quote-up-bg: "#fdeef0"
  quote-down: "#0c8050"
  quote-down-bg: "#e8f7f0"
  quote-flat: "#5b6c86"
  # 状态语义 — 与行情色分离，仅用于界面状态；色值已按 WCAG AA 校正
  state-ok: "#117e56"
  state-ok-bg: "#e6f5ee"
  state-warn: "#a36100"
  state-warn-bg: "#fff4e0"
  state-error: "#c8303c"
  state-error-bg: "#fdecee"
  state-info: "#2a55c8"
  state-info-bg: "#e8effc"
  state-busy: "#6a5ad2"
  state-busy-bg: "#efecfc"
  state-idle: "#5e6f88"
  state-idle-bg: "#eef1f6"
  # 语义面板描边 — tinted 面板（横幅、判决条、卡点块）的 1px 边，是 *-bg 的同色相压深
  state-ok-line: "#bfe4d4"
  state-info-line: "#c9daf5"
  state-warn-line: "#f0dcb4"
  state-error-line: "#f2c9cd"
  shadow-tint: "rgba(16, 39, 77, 1)"

typography:
  font-sans:
    fontFamily: "Inter, -apple-system, BlinkMacSystemFont, 'PingFang SC', 'HarmonyOS Sans SC', 'Microsoft YaHei', 'Source Han Sans SC', system-ui, sans-serif"
  font-num:
    fontFamily: "'JetBrains Mono', 'SF Mono', ui-monospace, Menlo, Consolas, monospace"
    fontFeature: tnum
  display:
    fontSize: 34px
    fontWeight: 700
    lineHeight: 1.25
    letterSpacing: -0.6px
  title-lg:
    fontSize: 24px
    fontWeight: 600
    lineHeight: 1.30
    letterSpacing: -0.4px
  title:
    fontSize: 19px
    fontWeight: 600
    lineHeight: 1.35
    letterSpacing: -0.2px
  title-sm:
    fontSize: 16px
    fontWeight: 600
    lineHeight: 1.40
    letterSpacing: -0.1px
  body:
    fontSize: 14px
    fontWeight: 400
    lineHeight: 1.60
    letterSpacing: 0
  body-sm:
    fontSize: 13px
    fontWeight: 400
    lineHeight: 1.55
    letterSpacing: 0
  caption:
    fontSize: 12px
    fontWeight: 400
    lineHeight: 1.50
    letterSpacing: 0
  micro:
    fontSize: 11px
    fontWeight: 500
    lineHeight: 1.40
    letterSpacing: 0.3px
  metric-xl:
    fontSize: 40px
    fontWeight: 700
    lineHeight: 1.10
    letterSpacing: -1.0px
    fontFeature: tnum
  metric:
    fontSize: 26px
    fontWeight: 700
    lineHeight: 1.15
    letterSpacing: -0.5px
    fontFeature: tnum
  metric-sm:
    fontSize: 20px
    fontWeight: 700
    lineHeight: 1.20
    letterSpacing: -0.3px
    fontFeature: tnum
  num:
    fontSize: 14px
    fontWeight: 500
    lineHeight: 1.50
    letterSpacing: 0
    fontFeature: tnum
  button:
    fontSize: 14px
    fontWeight: 500
    lineHeight: 1.20
    letterSpacing: 0

rounded:
  xs: 4px
  sm: 6px
  md: 8px
  lg: 12px
  xl: 16px
  pill: 9999px

spacing:
  xxs: 4px
  xs: 8px
  sm: 12px
  md: 16px
  lg: 20px
  xl: 24px
  xxl: 32px
  xxxl: 48px
  section: 64px

components:
  button-primary:
    backgroundColor: "{colors.brand-500}"
    textColor: "{colors.on-brand}"
    typography: "{typography.button}"
    rounded: "{rounded.md}"
    padding: 0 16px
    height: 36px
  button-primary-hover:
    backgroundColor: "{colors.brand-600}"
    textColor: "{colors.on-brand}"
    typography: "{typography.button}"
    rounded: "{rounded.md}"
  button-secondary:
    backgroundColor: "{colors.surface-1}"
    textColor: "{colors.ink-900}"
    typography: "{typography.button}"
    rounded: "{rounded.md}"
    padding: 0 16px
    height: 36px
  button-ghost:
    backgroundColor: transparent
    textColor: "{colors.ink-700}"
    typography: "{typography.button}"
    rounded: "{rounded.md}"
    padding: 0 12px
    height: 32px
  button-danger:
    backgroundColor: "{colors.surface-1}"
    textColor: "{colors.state-error}"
    typography: "{typography.button}"
    rounded: "{rounded.md}"
    padding: 0 16px
    height: 36px
  card:
    backgroundColor: "{colors.surface-1}"
    textColor: "{colors.ink-900}"
    typography: "{typography.body}"
    rounded: "{rounded.lg}"
    padding: 20px
  card-inset:
    backgroundColor: "{colors.surface-2}"
    textColor: "{colors.ink-900}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: 16px
  card-hero-action:
    backgroundColor: "{colors.navy-900}"
    textColor: "{colors.on-brand}"
    typography: "{typography.body}"
    rounded: "{rounded.lg}"
    padding: 24px
  metric-tile:
    backgroundColor: "{colors.surface-1}"
    textColor: "{colors.ink-900}"
    typography: "{typography.metric}"
    rounded: "{rounded.lg}"
    padding: 18px 20px
  text-input:
    backgroundColor: "{colors.surface-1}"
    textColor: "{colors.ink-900}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: 0 12px
    height: 36px
  status-badge:
    backgroundColor: "{colors.state-idle-bg}"
    textColor: "{colors.state-idle}"
    typography: "{typography.micro}"
    rounded: "{rounded.pill}"
    padding: 3px 10px
  nav-item:
    backgroundColor: transparent
    textColor: "{colors.ink-500}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: 0 12px
    height: 36px
  nav-item-active:
    backgroundColor: "{colors.brand-500}"
    textColor: "{colors.on-brand}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
  table-row:
    backgroundColor: "{colors.surface-1}"
    textColor: "{colors.ink-700}"
    typography: "{typography.num}"
    rounded: "{rounded.xs}"
    padding: 0 12px
    height: 40px
  table-header:
    backgroundColor: "{colors.surface-3}"
    textColor: "{colors.ink-500}"
    typography: "{typography.micro}"
    rounded: "{rounded.xs}"
    padding: 0 12px
    height: 36px
  onboarding-step:
    backgroundColor: "{colors.surface-2}"
    textColor: "{colors.ink-900}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: 14px 16px
  command-palette:
    backgroundColor: "{colors.surface-1}"
    textColor: "{colors.ink-900}"
    typography: "{typography.body}"
    rounded: "{rounded.lg}"
    padding: 8px
  detail-drawer:
    backgroundColor: "{colors.surface-1}"
    textColor: "{colors.ink-900}"
    typography: "{typography.body-sm}"
    rounded: "0"
    padding: 24px
---

> **分层与权威边界。** 本文档是**交互层**规范：信息架构、工作空间划分、依赖链模型、状态语言、文案对照、三层信息密度。
>
> **界面层**（组件视觉规格、全部状态矩阵、令牌数值、对比度审计实测值）的权威在 [`docs/prototype/quap-ui-kit.html`](prototype/quap-ui-kit.html) 的 `:root` 与组件展示台中；**判决面（c13）**的权威在 [`docs/prototype/quap-decision-surface.html`](prototype/quap-decision-surface.html)；界面层的判定规则与验收标准在 [`docs/UI-DESIGN.md`](UI-DESIGN.md)。
>
> 本文 front matter 中的色彩令牌，与 `docs/prototype/quap-interaction-design.html`、`docs/prototype/quap-decision-surface.html` 的 `:root`，都是 UI Kit 的**派生副本**。任何令牌变更必须在 UI Kit 中先改，再同步到这三处，并重跑 41 组对比度审计。四个产物中若出现色值冲突，以 UI Kit 为准。

## 1. Visual Theme & Atmosphere（视觉主题与氛围）

QUAP 是一个**单人操作员的 A 股研究工作台**。它的用户不是量化工程师，而是想看明白"今天市场怎么样、我的组合该怎么办"的普通研究者。因此这套设计系统的第一原则不是"炫技"，而是**降低认知门槛**：把系统内部概念翻译成人话，把该做的决定推到用户面前，把不该看的细节收起来。

视觉基调是**浅色专业风**——白底、深海军蓝锚点、大量留白、极少阴影。它参考了两条脉络：**Linear 的结构纪律**（四级表面阶梯、发丝边框、强调色极度稀缺、密集数据表、负字距的紧凑排版）与 **Stripe 的金融数据清晰度**（海军蓝墨色、表格数字、单一填充 CTA、克制的层级）。A 股研究工具的既有习惯（Wind、同花顺、东方财富）均为浅色，长时间盯盘阅读时浅色底比深色底更不易疲劳，这也是本项目在浅色与深色之间选择浅色的直接理由。

**核心视觉特征：**

- **海军蓝锚点** — `{colors.navy-900}` (#122340) 承载侧边栏与「下一步」主行动卡；正文墨色 `{colors.ink-900}` (#0e1c33) 也是海军蓝系，**全站不使用纯黑**，避免长时间阅读的硬对比疲劳。
- **单一强调色** — 靛蓝 `{colors.brand-500}` (#3567d6) 是系统里唯一的彩色强调，只出现在主按钮、导航选中态、聚焦环与链接。它**从不**作为卡片填充或区块背景。
- **红涨绿跌是硬约束** — `{colors.quote-up}` (#d32935) 与 `{colors.quote-down}` (#0c8050) 遵循中国市场惯例，与欧美相反，不可反转。行情色与界面状态色（成功/警告/错误）是两套独立色板，见第 2 章。
- **数字即主角** — 所有含金额、数量、涨跌幅、得分的单元格强制 `font-feature-settings: "tnum"` 等宽数字，并用 `{typography.font-num}` 渲染。这是金融产品的静默签名，也是让表格"能对齐、能扫读"的物理基础。
- **中文优先的排版** — 界面文案 100% 中文；拉丁字母与数字走 Inter，中文回落到苹方/鸿蒙/微软雅黑。字号以 14px 为正文基准（中文比拉丁文需要更大字号才能达到同等可读性），表格 13px。
- **中文行高更松** — 中文无词间空格，行高需要比拉丁文更松才能形成清晰的文字块。正文行高 1.60，标题 1.25–1.40。

**质感倾向：** 纯扁平 + 发丝边框为主，阴影仅用于浮层（抽屉、命令面板、下拉）。卡片靠 `{colors.hairline}` (#e3e9f3) 的 1px 边框与白底分离，不靠阴影。

## 2. Color Palette & Roles（调色板与角色）

### 品牌与强调（Brand & Accent）

| Token | HEX | 变量 | 使用场景 |
|---|---|---|---|
| `{colors.brand-700}` | #1d3f7d | `--brand-700` | 深色底上的链接、图表主色 |
| `{colors.brand-600}` | #2a55c8 | `--brand-600` | 主按钮 hover 态 |
| `{colors.brand-500}` | #3567d6 | `--brand-500` | **主色**：主按钮、导航选中、聚焦环、链接 |
| `{colors.brand-400}` | #6b93e6 | `--brand-400` | 图表次要序列、进度条填充 |
| `{colors.brand-200}` | #c7d8f7 | `--brand-200` | 进度条轨道、选中行底 |
| `{colors.brand-100}` | #e4ecfc | `--brand-100` | 信息类标签底、高亮行 |
| `{colors.brand-50}` | #f2f6fd | `--brand-50` | 侧边栏 hover 态、极轻高亮 |

### 深色面（Brand & Dark）

| Token | HEX | 变量 | 使用场景 |
|---|---|---|---|
| `{colors.navy-950}` | #0c182e | `--navy-950` | 侧边栏最底色、抽屉遮罩 |
| `{colors.navy-900}` | #122340 | `--navy-900` | **侧边栏**、`card-hero-action` 填充 |
| `{colors.navy-800}` | #1a3055 | `--navy-800` | 侧边栏 hover、深色面上的次级块 |
| `{colors.navy-700}` | #24406e | `--navy-700` | 侧边栏分隔线、深色面上的边框 |
| `{colors.on-brand}` | #ffffff | `--on-brand` | 深色面上的文字 |

### 文字（Neutral / Ink）

三级文字灰，全部通过 WCAG AA（最浅一级在页面底色上仍有 4.98:1）。**不要新增第四级文字灰**——低于 `{colors.ink-500}` 的灰在任何背景上都无法同时满足 4.5:1，加了就是无障碍缺陷。

| Token | HEX | 变量 | 对比度（白底 / 页面底） | 使用场景 |
|---|---|---|---|---|
| `{colors.ink-900}` | #0e1c33 | `--ink-900` | 17.04:1 / 15.89:1 | 标题、核心指标数字、正文强调 |
| `{colors.ink-700}` | #2b3c57 | `--ink-700` | 11.13:1 / 10.38:1 | 正文、表格单元格 |
| `{colors.ink-500}` | #5b6c86 | `--ink-500` | 5.34:1 / 4.98:1 | 次级说明、表头、时间戳、占位符 |
| `{colors.ink-200}` | #aab6c8 | `--ink-200` | 2.05:1（**非文字**） | **仅限分隔线、装饰性图标、禁用态填充——不承载任何文字** |

### 表面与边框（Surface & Borders）

| Token | HEX | 变量 | 使用场景 |
|---|---|---|---|
| `{colors.canvas}` | #f5f7fb | `--canvas` | 页面底色 |
| `{colors.surface-1}` | #ffffff | `--surface-1` | 卡片、表格、抽屉、浮层 |
| `{colors.surface-2}` | #fbfcfe | `--surface-2` | 卡片内嵌块、引导步骤、只读区 |
| `{colors.surface-3}` | #eef2f8 | `--surface-3` | 表格表头、分区底、骨架屏 |
| `{colors.hairline}` | #e3e9f3 | `--hairline` | 卡片边框、表格行分隔线 |
| `{colors.hairline-strong}` | #cfd8e6 | `--hairline-strong` | 输入框边框、聚焦前的强分隔 |

### 行情色（Quote — 红涨绿跌，不可反转）

色值已按 WCAG AA 校正，括号内为原值。压暗后仍保持明确的红/绿色相，在白底与页面底上均 ≥4.5:1。

| Token | HEX | 变量 | 对比度（白底 / 页面底） | 使用场景 |
|---|---|---|---|---|
| `{colors.quote-up}` | #d32935 | `--quote-up` | 5.07:1 / 4.73:1 | **涨**：涨幅数字、涨跌箭头、分时线上行 |
| `{colors.quote-up-bg}` | #fdeef0 | `--quote-up-bg` | — | 涨停标记底、涨方向标签底 |
| `{colors.quote-down}` | #0c8050 | `--quote-down` | 4.98:1 / 4.64:1 | **跌**：跌幅数字、跌箭头、分时线下行 |
| `{colors.quote-down-bg}` | #e8f7f0 | `--quote-down-bg` | — | 跌方向标签底 |
| `{colors.quote-flat}` | #5b6c86 | `--quote-flat` | 5.34:1 / 4.98:1 | **平**：0.00% 或数据缺失（显示为「—」） |

> **校正记录：** 涨红原为 #d93a45（页面底 4.40:1，不达标），跌绿原为 #0f9d63（白底 3.49:1，不达标），平灰原为 #8595ab（2.72:1，不达标）。三者均按「保持色相、仅压暗亮度」的方式调整到刚好达标，色相角未变。

### 状态语义色（Semantic — 与行情色严格分离）

六种状态全部通过 AA。徽标文字与其底色底的对比度均 ≥4.5:1。

| Token | HEX | 变量 | 徽标对比度 | 使用场景 |
|---|---|---|---|---|
| `{colors.state-ok}` | #117e56 | `--state-ok` | 4.50:1 | 正常、已完成、已就绪、心跳正常 |
| `{colors.state-ok-bg}` | #e6f5ee | `--state-ok-bg` | — | 上述状态的徽标底色 |
| `{colors.state-warn}` | #a36100 | `--state-warn` | 4.52:1 | 注意、待验证、覆盖不足、令牌将过期 |
| `{colors.state-warn-bg}` | #fff4e0 | `--state-warn-bg` | — | 警示横幅与徽标底色 |
| `{colors.state-error}` | #c8303c | `--state-error` | 4.67:1 | 失败、异常、连接断开 |
| `{colors.state-error-bg}` | #fdecee | `--state-error-bg` | — | 错误横幅与徽标底色 |
| `{colors.state-info}` | #2a55c8 | `--state-info` | 5.63:1 | 提示、说明、无操作性的信息 |
| `{colors.state-info-bg}` | #e8effc | `--state-info-bg` | — | 信息横幅与徽标底色 |
| `{colors.state-busy}` | #6a5ad2 | `--state-busy` | 4.51:1 | 执行中、排队中、运行中 |
| `{colors.state-busy-bg}` | #efecfc | `--state-busy-bg` | — | 进行中徽标底色 |
| `{colors.state-idle}` | #5e6f88 | `--state-idle` | 4.52:1 | 未开始、已归档、不适用 |
| `{colors.state-idle-bg}` | #eef1f6 | `--state-idle-bg` | — | 中性徽标底色 |

> **为什么中性徽标也是深灰：** 「未开始」读起来是"中性"靠的是**没有颜色**（相对其他五种的彩色），不是靠低对比度。把灰调浅到"看起来更弱"就会掉到 3:1 以下，成为无障碍缺陷。完整的审计数据见 `docs/UI-DESIGN.md` 第 6 章。

### 阴影色（Shadow Colors）

阴影统一使用海军蓝着色而非纯黑——纯黑阴影在浅灰底上会发脏，海军蓝阴影能让浮层"浮起来"而不是"糊上去"。

```css
--shadow-xs: 0 1px 2px rgba(16,39,77,.06);
--shadow-sm: 0 1px 3px rgba(16,39,77,.07), 0 1px 2px rgba(16,39,77,.04);
--shadow-md: 0 4px 12px rgba(16,39,77,.08), 0 1px 3px rgba(16,39,77,.05);
--shadow-lg: 0 12px 32px rgba(16,39,77,.10), 0 2px 8px rgba(16,39,77,.06);
--shadow-xl: 0 24px 64px rgba(16,39,77,.14), 0 4px 16px rgba(16,39,77,.07);
--shadow-focus: 0 0 0 3px rgba(53,103,214,.22);
```

## 3. Typography Rules（排版规则）

### 字体族

- **中文与正文** — `{typography.font-sans}`：`Inter, -apple-system, BlinkMacSystemFont, 'PingFang SC', 'HarmonyOS Sans SC', 'Microsoft YaHei', 'Source Han Sans SC', system-ui, sans-serif`。**Inter 置于苹方之前**是有意为之：让拉丁字母与阿拉伯数字走 Inter（字宽统一、数字造型清晰），中文自动回落到苹方。这个顺序是中文界面里提升数字可读性最省力的一招。
- **数字与代码** — `{typography.font-num}`：`'JetBrains Mono', 'SF Mono', ui-monospace, Menlo, Consolas, monospace`，配合 `font-feature-settings: "tnum"`。仅用于表格中的代码、编号、原始数据。

### 层级

| Token | Size | Weight | Line Height | Letter Spacing | 用途 |
|---|---|---|---|---|---|
| `{typography.display}` | 34px | 700 | 1.25 | -0.6px | 页面主标题（工作空间名） |
| `{typography.title-lg}` | 24px | 600 | 1.30 | -0.4px | 区块大标题 |
| `{typography.title}` | 19px | 600 | 1.35 | -0.2px | 卡片标题 |
| `{typography.title-sm}` | 16px | 600 | 1.40 | -0.1px | 小卡片标题、表单分组 |
| `{typography.body}` | 14px | 400 | 1.60 | 0 | 正文、导航、表单标签 |
| `{typography.body-sm}` | 13px | 400 | 1.55 | 0 | 表格单元格、辅助正文 |
| `{typography.caption}` | 12px | 400 | 1.50 | 0 | 说明文字、时间戳、数据口径 |
| `{typography.micro}` | 11px | 500 | 1.40 | 0.3px | 眉标、表格表头、徽标 |
| `{typography.metric-xl}` | 40px | 700 | 1.10 | -1.0px | 首屏核心指标 |
| `{typography.metric}` | 26px | 700 | 1.15 | -0.5px | 指标卡数字 |
| `{typography.metric-sm}` | 20px | 700 | 1.20 | -0.3px | 板块卡涨跌幅 |
| `{typography.num}` | 14px | 500 | 1.50 | 0 | 表格数字（tnum） |
| `{typography.button}` | 14px | 500 | 1.20 | 0 | 按钮标签 |

### 设计哲学

- **中文行高必须比拉丁文松。** 中文没有词间空格，词与词的边界靠字距与行距建立。正文行高 1.60、说明 1.50 是本系统的最小值；把它压到 1.4 会让中文段落糊成一片。
- **字号不能照搬拉丁方案。** 拉丁正文 16px 是常见值，但中文笔画密度高，14px 是中文网页正文的实际舒适区，13px 是密集表格的极限。低于 12px 的中文在任何屏幕上都不应出现。
- **标题字重停在 600。** 苹方的 Bold(700) 用于中文标题会显得笨重；600(Semibold) 已足够建立层级。只有数字型指标（`metric-*`）与页面主标题（`display`）使用 700，因为数字需要更重的笔画在浅底上"站住"。
- **负字距只给大字号。** `display` 到 `title` 使用 -0.6px 至 -0.1px 的负字距收紧，`body` 及以下保持 0。中文在小字号下负字距会挤压笔画导致糊字。
- **数字一律等宽。** 表格、指标卡、涨跌幅、日期全部启用 `tnum`。这是表格能否被"扫读"而非"逐行读"的前提。

## 4. Component Stylings（组件样式）

### 按钮（Buttons）

**`button-primary`** — 主行动按钮。每个视口**最多出现一个**。
```css
.btn-primary{background:var(--brand-500);color:#fff;font:500 14px/1.2 var(--font-sans);
  height:36px;padding:0 16px;border:0;border-radius:8px;transition:background .15s;}
.btn-primary:hover{background:var(--brand-600);}
.btn-primary:focus-visible{outline:none;box-shadow:var(--shadow-focus);}
.btn-primary:disabled{background:var(--surface-3);color:var(--ink-200);cursor:not-allowed;}
```

**`button-secondary`** — 次级操作，白底 + 发丝边框。
```css
.btn-secondary{background:var(--surface-1);color:var(--ink-900);font:500 14px/1.2 var(--font-sans);
  height:36px;padding:0 16px;border:1px solid var(--hairline-strong);border-radius:8px;}
.btn-secondary:hover{background:var(--surface-2);border-color:var(--brand-400);color:var(--brand-600);}
```

**`button-ghost`** — 纯文字按钮，用于卡片内低权重操作（如「查看全部」）。高 32px，无边框无底色，hover 时底色 `{colors.surface-3}`。

**`button-danger`** — 危险操作。白底 + 红字 + 红边框，**不使用红色填充**——红色填充在浅色底上过于刺眼，且会与「涨」的红色混淆。

### 卡片（Cards）

**`card`** — 基础容器。白底、1px `{colors.hairline}` 边框、12px 圆角、20px 内边距、无阴影。
```css
.card{background:var(--surface-1);border:1px solid var(--hairline);border-radius:12px;padding:20px;}
```

**`card-inset`** — 卡片内部的次级块（只读信息、引导步骤）。底色 `{colors.surface-2}`，8px 圆角，16px 内边距，无边框。

**`card-hero-action`** — 「下一步」主行动卡。深海军蓝 `{colors.navy-900}` 填充 + 白字 + 24px 内边距。**全站唯一使用深色填充的卡片**，用于把「系统建议你现在做什么」推到视觉最前。

**`metric-tile`** — 指标磁贴。白底、12px 圆角、18px 20px 内边距。结构固定为四行：眉标（`micro`）→ 数字（`metric`）→ 单位/口径（`caption`）→ 环比（可选）。

### 输入（Inputs）

```css
.input{background:var(--surface-1);color:var(--ink-900);font:400 14px/1.5 var(--font-sans);
  height:36px;padding:0 12px;border:1px solid var(--hairline-strong);border-radius:8px;width:100%;}
.input::placeholder{color:var(--ink-500);}
.input:focus{outline:none;border-color:var(--brand-500);box-shadow:var(--shadow-focus);}
.input[aria-invalid="true"]{border-color:var(--state-error);}
```

输入框**必须带可见标签**（14px / `{colors.ink-700}` / 距输入框 6px），不使用「占位符即标签」的反模式——占位符在输入后消失，用户会忘记这个字段是什么。字段级说明放在标签右侧的问号气泡内。

### 导航（Navigation）

**侧边栏** — 248px 固定宽，`{colors.navy-900}` 填充。分三组（日常 / 研究 / 系统），组间用 12px 间距 + `micro` 组标题分隔。深色底上的文字使用深色面专用令牌，不复用浅色底灰阶：未选中项 `#a8bcd9`（8.10:1）、hover `#dce6f5`（10.44:1）、组标题与脚注 `#7b8fb3`（4.79:1）、选中项纯白（15.67:1）。

```css
.nav-item{display:flex;align-items:center;gap:10px;height:36px;padding:0 12px;border-radius:8px;
  color:#a8bcd9;font:400 14px/1.2 var(--font-sans);cursor:pointer;transition:background .15s;}
.nav-item:hover{background:var(--navy-800);color:#dce6f5;}
.nav-item.is-active{background:var(--brand-500);color:#fff;font-weight:500;}
```

**选中态是实心靛蓝块**，不是左侧竖条——实心块在深色底上的可扫描性远高于 2px 竖条。

### 徽标与标签（Badges / Tags）

**`status-badge`** — 统一状态徽标。`{rounded.pill}`、`{typography.micro}`、3px 10px 内边距、前置 5px 圆点。颜色取第 2 章状态语义色板，**同一个状态在全站必须使用同一个色板组合**。

**`tag`** — 分类标签（板块、类型）。4px 圆角（比徽标更方，以区分"属性"与"状态"）、`{colors.surface-3}` 底、`{colors.ink-500}` 字。

**`tag-up` / `tag-down`** — 方向标签，用于表达"调整方向"而非"系统状态"：加仓、上调权重用 `tag-up`（`{colors.quote-up-bg}` 底 / `{colors.quote-up}` 字），减仓、下调权重用 `tag-down`（`{colors.quote-down-bg}` 底 / `{colors.quote-down}` 字）。

**为什么需要方向标签：** 「建议加仓」既不是"正常"也不是"注意"，把它塞进状态徽标会污染状态语言。方向标签复用行情色板表达"向上/向下"，与同一行的调整幅度数字颜色保持一致，用户扫一眼就能对上。中性动作（维持、保留现金）仍用 `tag` 或 `badge.idle`。

**`badge.ok` 只用于"健康/完成"，不用于"看涨"。** 「可交易」「已完成」「心跳正常」用 `badge.ok`；「建议加仓」用 `tag-up`。两者不可互换。

### 表格（Tables）

```css
.table{width:100%;border-collapse:separate;border-spacing:0;font:500 13px/1.5 var(--font-num);}
.table thead th{background:var(--surface-3);color:var(--ink-500);font:500 11px/1.4 var(--font-sans);
  letter-spacing:.3px;height:36px;padding:0 12px;text-align:left;position:sticky;top:0;}
.table thead th:first-child{border-radius:8px 0 0 8px;}
.table thead th:last-child{border-radius:0 8px 8px 0;}
.table tbody td{height:40px;padding:0 12px;color:var(--ink-700);border-bottom:1px solid var(--hairline);}
.table tbody tr:hover td{background:var(--brand-50);}
.table .num{text-align:right;font-variant-numeric:tabular-nums;}
```

数值列**一律右对齐**并加 `.num` 类。文字列左对齐。涨跌幅列根据符号附加 `.up` / `.down` / `.flat` 类，颜色取 `{colors.quote-*}`。

### 浮层（Modals / Drawers）

**`command-palette`** — ⌘K 命令面板。居中浮层，`{rounded.lg}` 12px 圆角、`--shadow-xl`、8px 内边距。最大宽 560px，最大高 60vh。遮罩 `rgba(12,24,46,.42)` + `backdrop-filter: blur(2px)`。

**`detail-drawer`** — 右侧详情抽屉。宽 480px，白底，仅左侧 1px 边框（`{colors.hairline}`），`--shadow-xl`，`transform: translateX(100%) → 0` 过渡 240ms `cubic-bezier(.32,.72,0,1)`。**用于承载 L3 明细层**（原始 JSON、溯源链路），替代原先散落各处的 `st.expander`。

### 引导组件（Onboarding）

**`onboarding-step`** — 新手引导单步。结构：状态圆点（24px，完成时 `{colors.state-ok}` 实心 + 白色对勾，进行中 `{colors.brand-500}` 描边，未开始 `{colors.surface-3}` 实心）→ 步骤标题（`{typography.title-sm}`）→ 一句话说明（`{typography.body-sm}` / `{colors.ink-500}`）→ 右侧操作按钮（仅当前步骤显示主按钮）。已完成的步骤整行降透明度至 .55。

## 5. Layout Principles（布局原则）

### 间距系统

基数 **4px**，令牌：`{spacing.xxs}` 4 · `{spacing.xs}` 8 · `{spacing.sm}` 12 · `{spacing.md}` 16 · `{spacing.lg}` 20 · `{spacing.xl}` 24 · `{spacing.xxl}` 32 · `{spacing.xxxl}` 48 · `{spacing.section}` 64。

- 卡片内边距：20px（标准）/ 16px（内嵌块）/ 24px（主行动卡）。
- 卡片间距：16px（同组）/ 24px（跨组）。
- 区块间距：32px；页面上下留白 32px。
- 表单字段垂直间距：16px；字段标签到输入框 6px。

### 网格与容器

- 内容容器 `max-width: 1440px`，水平内边距 32px（≥1024px）/ 20px（<1024px）/ 16px（<768px）。
- 12 列栅格，列间距 16px。
- 常用布局：指标区 4 列 → 平板 2 列 → 手机 1 列；板块卡 3 列 → 平板 2 列 → 手机 1 列。
- 侧边栏 248px 固定；主内容区独立滚动。

### 三层信息密度（渐进式披露）

这是本系统**最重要的布局原则**，直接解决"信息密度失控"的问题。每一屏的信息必须按三层组织：

| 层级 | 内容 | 默认状态 | 载体 |
|---|---|---|---|
| **L1 结论层** | 一句话结论 + 1 个核心数字 | 始终可见 | 主行动卡、指标磁贴 |
| **L2 依据层** | 支撑结论的紧凑表格 / 图表 | 可见，可折叠 | `card` + `table` |
| **L3 明细层** | 原始 JSON、溯源链路、接口明细 | **默认收起** | `detail-drawer` |

**L3 永远不直接铺在页面上。** 所有 `st.json()` 级别的原始数据必须进入抽屉，由「查看原始数据」按钮触发。这是把当前系统从"开发者调试界面"变成"产品"的关键一步。

### 留白哲学

留白用来**建立区块的边界感**，而不是填充空白。同组卡片之间用 16px 紧凑间距形成"块"，跨组之间用 32px 拉开，用户靠间距就能读出信息分组，无需额外的分割线。卡片内部不吝惜留白——20px 内边距是下限。

## 6. Depth & Elevation（深度与层级）

| 层级 | 处理方式 | 用途 |
|---|---|---|
| 0（贴底） | 无阴影无边框 | 正文、页面底色 |
| 1（发丝分离） | `{colors.surface-1}` 底 + 1px `{colors.hairline}` 边框 | **默认卡片、表格容器** |
| 2（内嵌） | `{colors.surface-2}` 底，无边框 | 卡片内的次级块、只读区 |
| 3（微浮） | `--shadow-sm` | 下拉菜单、气泡、悬浮提示 |
| 4（浮层） | `--shadow-lg` | 右侧抽屉 |
| 5（模态） | `--shadow-xl` + 遮罩 `rgba(12,24,46,.42)` | 命令面板、确认对话框 |

**表面阶梯：** `{colors.canvas}` → `{colors.surface-1}` → `{colors.surface-2}` → `{colors.surface-3}`。层级提升**优先用表面阶梯与发丝边框表达，而非阴影**。阴影只在元素真正脱离文档流（浮层）时出现——这是 Linear 的核心纪律，也是让浅色界面保持"干净"而非"发灰"的关键。

**Z-index 规范：**

| 值 | 用途 |
|---|---|
| 0 | 内容 |
| 10 | 粘性表头、粘性顶栏 |
| 100 | 下拉、气泡 |
| 500 | 侧边栏（移动端） |
| 1000 | 抽屉 |
| 2000 | 命令面板、对话框 |
| 2100 | 全局提示（Toast） |

**背板效果：** 遮罩使用 `rgba(12,24,46,.42)` + `backdrop-filter: blur(2px)`。模糊值刻意保持很小——大模糊会让背后的数据表变成一团色块，用户会失去空间定位感。

## 7. Do's and Don'ts（设计规范与禁忌）

### Do（必须）

1. **界面文案 100% 中文。** 包括导航名、按钮、表单标签、空状态、错误提示、图表标题、表头。代码标识符与 API 字段名可以保留英文，但**必须**包在等宽字体里并放在 L3 明细层。
2. **技术术语必须配解释。** 任何量化术语（IC、Rank IC、分位、影子期、基线权重、不可变代次）在首次出现处提供问号气泡，用一句人话说明「它是什么、看它有什么用」。
3. **红涨绿跌。** `{colors.quote-up}` 用于涨，`{colors.quote-down}` 用于跌。这是中国市场惯例，与欧美相反，绝不可因参考了欧美设计系统而反转。
4. **所有数字等宽。** 金额、数量、涨跌幅、得分、日期一律 `tnum`，数值列右对齐。
5. **每个视口最多一个主按钮。** 用层级（primary / secondary / ghost）表达操作优先级，不要用两个同权重按钮让用户猜。
6. **L3 明细默认收起。** 原始 JSON、溯源链路、接口明细一律进抽屉。
7. **空状态必须给出下一步。** 空状态 = 一句话说明「为什么还没有数据」+ 一个可点的操作，而不是一句"暂无数据"。
8. **状态用统一徽标。** 全站状态色板只有六套（正常/注意/错误/信息/进行中/中性），同一状态必须同色。

### Don't（禁止）

1. **不要把 `{colors.brand-500}` 用作卡片填充或区块背景。** 它是强调色，只给主按钮、导航选中、聚焦环、链接。用大面积靛蓝会立刻稀释掉它的指示作用。
2. **不要反转涨跌色。** 绿涨红跌是欧美惯例，在中国 A 股产品里会让用户读出完全相反的含义。
3. **不要把行情色当状态色用。** 「跌 3%」的绿和「运行正常」的绿虽然同族但职责不同，不要交叉引用变量。
4. **不要给非涨跌量上行情色。** 「综合得分 1.842」「Rank IC 0.0482」「动量 0.182」都是统计量，不是价格变动，一律用 `{colors.ink-900}` 中性色。它们的"好坏"由相邻的状态徽标（有效 / 偏弱 / 无预测力）表达，不由颜色表达。**只有涨跌幅、收益率、权重调整幅度使用红绿。**
5. **不要用纯黑 `#000000`。** 文字用 `{colors.ink-900}` (#0e1c33)，阴影用海军蓝着色。
6. **不要给卡片加阴影。** 卡片靠边框分离。给所有卡片加阴影会让页面在浅色底上迅速变脏。
7. **不要在正文里使用英文缩写作为标签。** 写「综合得分」不写「score」，写「有效覆盖率」不写「coverage」，写「复权因子」不写「factor」。
8. **不要把中文压到 12px 以下。** 12px 是中文的绝对下限，只用于时间戳与脚注。
9. **不要用红色填充危险按钮。** 危险操作使用白底红字红边框。
10. **不要用占位符代替标签。** 每个输入框必须有常驻可见标签。

## 8. Responsive Behavior（响应式行为）

### 断点

| 名称 | 宽度 | 关键变化 |
|---|---|---|
| 宽屏 | ≥ 1600px | 内容容器封顶 1440px，指标区 4 列 |
| 桌面 | 1024–1599px | 默认布局，指标区 4 列，板块卡 3 列 |
| 平板 | 768–1023px | 指标区 2 列，板块卡 2 列，侧边栏收为 64px 图标条 |
| 手机 | < 768px | 单列，侧边栏改为底部标签栏，表格横向滚动 |

### 触摸目标

- 主/次按钮高度 ≥ 36px，触摸设备提升至 44px。
- 导航项高度 36px，触摸设备 44px。
- 表格行高 40px 保证可点。
- 图标按钮点击区 ≥ 32×32px，视觉尺寸可更小。

### 折叠策略

- **侧边栏**：< 1024px 收为 64px 图标条（保留图标 + tooltip）；< 768px 改为底部 5 项标签栏（今日概览 / 组合 / 选股 / 研究 / 更多）。
- **指标区**：4 → 2 → 1 列阶梯。
- **表格**：< 768px 保留首两列（代码、名称）与数值列，其余列折叠进行内「详情」抽屉；容器横向可滚动且首列 sticky。
- **抽屉**：< 768px 从右侧抽屉改为自底部升起的面板，高度 80vh。
- **字号**：`display` 34 → 28 → 24px；`metric-xl` 40 → 32 → 28px。正文与表格字号**不缩放**（14px / 13px 已是中文下限）。

### 字体缩放

中文在移动端不降字号，仅降低信息密度（减少每屏条目数）。用户系统字号放大时，行高按比例放大以保证中文行间可读。

## 9. Agent Prompt Guide（AI 代理提示指南）

### Quick Reference

```
主色 #3567d6 · 深色面 #122340 · 页面底 #f5f7fb · 卡片 #ffffff · 边框 #e3e9f3
正文 #2b3c57 · 标题 #0e1c33 · 次级 #5b6c86 · 非文字 #aab6c8（不可承载文字）
涨 #d32935（红）· 跌 #0c8050（绿）· 平 #5b6c86
状态：正常 #117e56 · 注意 #a36100 · 错误 #c8303c · 信息 #2a55c8 · 进行中 #6a5ad2 · 中性 #5e6f88
深色面：底 #122340 · 未选中 #a8bcd9 · hover #dce6f5 · 脚注 #7b8fb3
字体 Inter → PingFang SC；数字 JetBrains Mono + tnum
圆角 4/6/8/12/16 · 间距基数 4px · 卡片内边距 20px · 卡片间距 16px
阴影仅用于浮层：--shadow-lg（抽屉）· --shadow-xl（模态）
卡片用 1px 边框不用阴影 · 强调色极度稀缺 · 界面 100% 中文
```

### Component Prompts（可直接复制）

**1. 生成一个指标磁贴**
> 按 QUAP 设计系统生成指标磁贴：白底、1px `--hairline` 边框、12px 圆角、18px 20px 内边距。四行结构：眉标用 11px/500 字重 `--ink-500`；数字用 26px/700 字重、`letter-spacing:-0.5px`、`font-variant-numeric:tabular-nums`、`--ink-900`；口径说明用 12px `--ink-500`；环比涨跌幅用 13px/500，正数加 `--quote-up`，负数加 `--quote-down`，零值加 `--quote-flat`。

**2. 生成一个统一状态徽标**
> 生成状态徽标：`border-radius:9999px`、`padding:3px 10px`、字号 11px 字重 500、前置 5px 实心圆点。状态到色板的映射：正常 `#117e56`/`#e6f5ee`、注意 `#a36100`/`#fff4e0`、错误 `#c8303c`/`#fdecee`、信息 `#2a55c8`/`#e8effc`、进行中 `#6a5ad2`/`#efecfc`、中性 `#5e6f88`/`#eef1f6`。所有组合均已验证 ≥4.5:1。

**3. 生成新手引导步骤条**
> 生成 4 步新手引导：每步一行，左侧 24px 状态圆点（已完成=`--state-ok` 实心带白色对勾；进行中=`--brand-500` 描边空心；未开始=`--surface-3` 实心），中间是步骤标题（16px/600）+ 一句话说明（13px `--ink-500`），右侧仅当前步骤显示主按钮。已完成步骤整行 `opacity:.55`。

**4. 生成数据表格**
> 生成表格：容器白底 + 1px `--hairline` 边框 + 12px 圆角。表头 `--surface-3` 底、11px/500 字重、`--ink-500`、高 36px、首尾单元格 8px 圆角。数据行高 40px、13px 字重 500、`--ink-700`、行分隔线 1px `--hairline`、hover 底色 `--brand-50`。数值列加 `text-align:right;font-variant-numeric:tabular-nums`。涨跌幅按符号着色。

**5. 生成右侧详情抽屉**
> 生成右侧抽屉：宽 480px、白底、仅左侧 1px `--hairline` 边框、`box-shadow:var(--shadow-xl)`。初始 `transform:translateX(100%)`，打开时 `translateX(0)`，过渡 240ms `cubic-bezier(.32,.72,0,1)`。遮罩 `rgba(12,24,46,.42)` + `backdrop-filter:blur(2px)`。抽屉顶部标题 19px/600 + 关闭按钮，内容区放原始 JSON（等宽字体、13px）与溯源链路。

### Iteration Guide（迭代建议）

1. **一次只改一个组件**，并直接引用令牌名（`{colors.brand-500}`、`{button-primary}-hover`）而不是写死色值。
2. **新增状态前先检查六套状态色板能否覆盖**。覆盖不了再考虑扩展，不要为单个页面新建色。
3. **新增区块前先决定它属于哪一层**（L1 结论 / L2 依据 / L3 明细）。归不进 L1 或 L2 的一律进抽屉。
4. **任何新增文案先问"普通用户看得懂吗"**。看不懂就换成人话，术语塞进问号气泡。
5. **新页面必须能回答"我接下来该做什么"**。答不出来说明缺了主行动卡。
6. **修改配色后跑一遍涨跌色检查**：涨必须是红、跌必须是绿。
7. **表格列超过 7 列时，考虑拆成两个层级或加列筛选**，不要靠横向滚动解决。
8. **每次改动后同步更新本文件的组件章节**，保持规范与实现一致——这是"方便后续升级迭代"的前提。

---

## 附录 A：信息架构 —— 从「按系统模块」到「按操作员决策」

> 可点击原型：[`docs/prototype/quap-decision-surface.html`](prototype/quap-decision-surface.html)（含 4 个判决场景 + 改造前对照视图）

### A.0 问题的实质：不是「不好看」，是「按系统模块组织」

按用户目标分三层（日常 / 研究 / 系统）解决了「中英混杂」和「7 个平级模块」，但**没有解决往返**：

> 操作员每天只问一个问题——**今天能不能出建议？** 而现在要回答它，必须横跨 Overview（看到 Blocked）→ Data & Pipeline（找原因）→ Models & Validation（训练）→ 再回 Overview。三个空间的一次往返，没有引导。

根因三条，全部可在源码中定位：

| # | 机制 | 源码位置 | 后果 |
|---|---|---|---|
| 1 | `blockers` 是**平铺数组**，一次抛出 3–5 条 | `src/quant_platform/pipeline.py` | 操作员看到 5 个问题，实际是 1 个问题被摊成 5 行 |
| 2 | 其中下游几条是上游卡点的**必然后果** | 同上 | 修完根因回来仍显示 Blocked，以为没修好 |
| 3 | 成功提示**主动把用户送走** | `dashboard/workflow.py`：`st.success("Run … Follow progress in Data & Pipeline.")` | 每修一步都要离开当前空间 |

第 2 条是往返的真正机制。`no approved, qualified, unexpired Qlib model` 与 `no validated Qlib generation` 都是 `unverified capabilities` 的下游结果，**它们不是三个问题，是一个问题被摊成了三行**。

补充一处源码事实：`pipeline.py` 中「模型」检查（`models[freq] is None`）排在「数据代次」检查（`not any(g["frequency"] == freq …)`）**之前**，但真实依赖是「先有代次、才谈得上模型」。即**抛出顺序 ≠ 依赖顺序**，这是平铺列表误导操作员的又一个来源。

### A.1 设计判据

| 判据 | 落地 |
|---|---|
| 一级导航回答「我想做什么决定」，而非「系统有哪几个部件」 | 「今日」置顶为唯一判决入口 |
| 「今天能不能出建议」**0 次跳转** | 一屏给出判决句 + 解锁链路 |
| 判决状态**常驻** | 侧边栏常驻判决块（`.verdict-mini`，置于品牌区下、导航上），任何空间可见。**刻意不放进导航项**——导航项选中底 `--brand-500` 会让红/绿标记失效（见 UI-DESIGN 2.5） |
| 卡点**就地解决** | 解锁动作内联在卡点下方，不再把人送去另一个空间 |
| 下游**不显示自身失败状态** | 统一显示「等待上游」 |

### A.2 新结构

| 层级 | 工作空间 | 回答的问题 | 由谁而来 |
|---|---|---|---|
| 日常 | **今日** | 今天能不能出建议？不能的话卡在哪、我该做什么？ | Overview（重构为判决面） |
| 日常 | 我的组合 | 我的组合现在什么状态？要不要采纳？ | My Model Portfolio |
| 日常 | 选股发现 | 模型今天看好哪些股票？ | Low-Price Scan |
| 研究 | 个股研究 | 这只股票怎么样？模型怎么看？ | Stock Research |
| 研究 | 模型与报告 | 模型表现如何？因子有没有效？ | Qlib Report + Models & Validation |
| 系统 | **运行记录与配置** | 历史上跑过什么？当前配置是什么？ | Data & Pipeline（降级为只读） |

**关键改动：**

1. **「今日」= 判决面，不是仪表盘。** 首屏是一个判决条 + 一条解锁链路，只回答两件事：能不能出、卡在哪。
2. **「数据与任务」→「运行记录与配置」，降为只读。** 采集、训练这些**动作**不再从它发起——动作已内联到「今日」的卡点里。它只负责「查看历史运行」与「查看/修改配置」。这条改动直接消灭了「去 Data & Pipeline 找原因」这一跳。
3. **侧边栏用判决状态点代替徽标计数**，让判决在任何空间可见。

### A.3 依赖链模型：把平铺数组还原成链

`pipeline.py` 的 6 条 blocker 字符串对应 6 个门禁，分三个阶段。**阶段一的三项互相独立**（都直接对原始状态判定，不存在谁依赖谁），**阶段二起是顺序依赖**。

| # | 阶段 | 门禁 | 源码判定条件 | 依赖 |
|---|---|---|---|---|
| ① | 一 · 基础就绪 | 数据源验证 | `unverified capabilities: …`（该能力无成功采集记录） | 独立 |
| ② | 一 · 基础就绪 | 采集范围 | `settings.history_scope != "all"` | 独立 |
| ③ | 一 · 基础就绪 | Qlib 引擎 | `role not in roles or "qlib-data" not in roles` | 独立 |
| ④ | 二 · 数据与模型 | 数据代次 | `not any(g["frequency"] == freq for g in generations)` | 依赖 ①②③ |
| ⑤ | 二 · 数据与模型 | 模型发布 | `models[freq] is None`（需 `state='active' AND expires_at > now() AND e.passed`） | 依赖 ④ |
| ⑥ | 三 · 产出 | 今日建议 | 无 `VALID_REPORT` | 依赖 ⑤ |

**呈现规则（五条，全部为实现约束）：**

1. **阶段一全部展示真实状态。** 三项独立，可能同时是卡点——都展示，因为它们都可操作。**不得**把它们画成顺序依赖。
2. **阶段二起只呈现第一个卡点**，其余显示「等待上游」，**不显示自身失败状态**。这是消灭往返的核心规则。
3. **连接轨只在真依赖处绘制。** 阶段一内部不画轨（画了就是在说「① 之后才是 ②」）；阶段二内部画轨（④→⑤ 是真依赖）；跨阶段不画轨，顺序由阶段标签承载。
4. **阶段标签须声明依赖性质**：阶段一标「3 项互相独立」，阶段二/三标「顺序依赖」。
5. **无「假按钮」。** 若卡点在平台内不可解，不给按钮，给指令 + 「我已处理，重新检测」。

### A.4 卡点 → 动作类型三分

每个卡点必须归入三类之一，决定它下方出现什么：

| 动作类型 | 标识 | 判据 | 呈现 |
|---|---|---|---|
| **可自解** | `tag-auto`（信息蓝） | 平台内有对应操作，点了就推进 | 主按钮（如「立即采集」）+ 预计耗时 |
| **半自解** | `tag-manual`（注意黄） | 需改配置 / 重启平台进程 | 改什么 + 改完点「我已处理，重新检测」 |
| **需运维** | `tag-manual`（注意黄） | 卡点在平台进程之外（另一台机器、容器编排） | 可复制指令 + 「我已处理，重新检测」+ 明确写出「不做修复按钮」 |

**禁止**：给「需运维」类卡点配一个点了不起作用的按钮。这是现有实现最伤信任的一处——`Update source data` 对引擎离线毫无作用。

**「我已处理，重新检测」回路**：不自动轮询（避免误以为已恢复），由操作员确认后重跑判决；重跑期间按钮进入 `state-busy`。

### A.5 blocker 字符串 → 判决句（文案映射）

`blockers` 里的字符串是**机器可读原因，不得直接展示**。每个字符串映射为一句动作导向的中文，并附带唯一动作。

| 源码字符串 | 判决句（`verdict-reason`） | 动作类型 |
|---|---|---|
| `unverified capabilities: history, factors` | 卡在**数据源验证**：7 个日线接口里有 2 个从未成功完成一次真实采集，因此平台不承认这份数据，也就不会拿它训练或推理。 | 可自解 |
| `history scope is '…'; production acceptance requires …` | 卡在**采集范围**：当前只采了部分市场，生产验收要求全市场 ≥95% 覆盖。 | 半自解 |
| `required Qlib workers unavailable` | 卡在 **Qlib 引擎**：三个工作进程都没有心跳，训练与推理都无法执行。**这一步在平台里点不出来，需要有人去启动服务。** | 需运维 |
| `no validated Qlib generation` | 卡在**数据代次**：今天还没有一份通过验证的数据导出。 | 可自解 |
| `no approved, qualified, unexpired Qlib model` | 卡在**模型发布**：没有一份「已评估通过 + 已批准 + 未过期」的模型。 | 半自解 |
| `daily model baseline unavailable` | 五分钟叠加的独立卡点，**不影响日线判决** | 需运维 |

注意：⑤ 的判决句只在 ④ 已通过时才成为判决句；否则 ⑤ 显示「等待上游」。同理 ⑥。

**5 分钟叠加的表达**：五分钟受阻**不进入日线判决句**，只在判决条底部作为一行徽标出现（「五分钟叠加：同样受阻 · 不影响日线建议的解锁」）。两条频率的判决必须解耦，否则操作员会以为日线也被拖住了。

### A.6 侧边栏职责重划

| 工作空间 | 改造前职责 | 改造后职责 | 变化 |
|---|---|---|---|
| 今日 | 展示状态 + 抛警告 | **判决 + 就地解锁** | 由「报告」变「行动」 |
| 数据与任务 | 采集控制、接口验证、运行设置（可写） | **运行记录与配置（只读）** | 写操作全部迁出 |
| 模型与报告 | 训练候选模型、发布（可写） | 训练/发布入口保留在卡点内联处，本空间只读 | 写操作部分迁出 |

**一句话原则**：**能改变判决的动作，只能从判决面发起。**

### A.7 文案对照（英文 → 中文）

| 现文案 | 新文案 |
|---|---|
| Overview / "Are daily and five-minute recommendations ready?" | 今日 / "今天能不能出建议，以及为什么" |
| My Model Portfolio | 我的组合 |
| Stock Research | 个股研究 |
| Qlib Report | 模型与报告 |
| Low-Price Scan | 选股发现 |
| Models & Validation | 模型与报告 › 验证与发布 |
| Data & Pipeline | 运行记录与配置 |
| Collection controls / Update source data | 数据采集 / 立即采集 |
| Check provider capabilities | 检查数据源 |
| Model portfolio / Cash fraction / Target fraction | 我的组合 / 现金比例 / 目标权重 |
| Save next-session baseline | 保存为下一交易日基线 |
| Changes to accept | 本次采纳的调整 |
| Daily proposal / Five-minute overlay | 日线建议 / 五分钟叠加 |
| Search stocks / Stock code | 搜索股票 / 股票代码 |
| Portfolio context / Standalone model view | 组合上下文 / 独立模型视角 |
| Train challenger / Inspect model | 训练候选模型 / 查看模型详情 |
| Pipeline run / Retry same immutable inputs | 流水线运行 / 用相同输入重试 |
| Immutable Qlib generations | 不可变数据代次 |
| Proposed cash / Ready / Blocked | 建议现金比例 / 已就绪 / 受阻 |
| unverified capabilities: … | 卡在数据源验证：…（见 A.5 映射表） |
| no approved, qualified, unexpired Qlib model | 卡在模型发布：…（见 A.5） |


## 附录 B：统一状态语言

现有实现混用 `st.success` / `st.warning` / `st.error` / `st.info` 与自绘 pill，同一含义有多种表达。新设计收敛为**六种状态 + 三种载体**：

| 状态 | 色板 | 语义 | 出现场景示例 |
|---|---|---|---|
| 正常 | `state-ok` | 一切就绪 | API 已连接、行情覆盖完整、心跳正常 |
| 注意 | `state-warn` | 需要关注但可继续 | 行情覆盖不足、待验证接口、令牌将过期 |
| 错误 | `state-error` | 阻断当前操作 | 连接断开、令牌失效、任务执行失败 |
| 信息 | `state-info` | 中性说明 | 数据口径说明、免责声明 |
| 进行中 | `state-busy` | 后台处理中 | 采集执行中、报告生成中、排队中 |
| 中性 | `state-idle` | 未开始 / 不适用 | 等待执行、已归档、无数据 |

**三种载体（按严重度递进）：**

1. **徽标**（`status-badge`）— 行内状态，用于顶栏、表格状态列、卡片角标。占位最小，不打断阅读。
2. **横幅**（`banner`）— 页面级状态，横跨内容区，带图标 + 一句话说明 + 可选操作。仅「注意」与「错误」使用。
3. **Toast** — 操作结果反馈，2 秒自动消失，右下角。仅用于「操作已提交」这类即时反馈。

**顶栏常驻状态**：API 连接、数据源、行情覆盖、采集调度、更新时间，共 5 项，收敛为一行徽标。**不再在页面主体重复展示这些信息**。

> **顶栏徽标不得表达判决。** 这是本轮新增的硬约束。反例：判决条写着「今天出不了建议」（红），顶栏却挂着绿色「系统正常」——操作员立刻会问「系统正常为什么出不了建议」。顶栏只放**与判决无关的事实**（连通性），推荐文案「接口已连接」，用 `state-idle` 中性色，不用 `state-ok`。「今天能不能出建议」的唯一权威表述在判决条。

## 附录 C：三层信息密度落地对照

| 现在（铺开） | 现在位置 | 新位置 |
|---|---|---|
| 5 个状态 pill + 4 个指标 + 板块卡 | 概览页顶部 | L1 结论层（保留，收敛为 1 行状态 + 4 磁贴） |
| 「接口验证详情」`st.json` | 服务与接口页签 | L3 → 「数据源明细」抽屉 |
| 「运行指标 · 原始数据」`st.json` | 运行设置页签 | L3 → 「系统指标」抽屉 |
| 「历史采集、备份与 Qlib 状态」`st.json` | 运行设置页签 | L3 → 「系统指标」抽屉 |
| Provenance 链路 `st.json` | 每个提案卡内 | L3 → 「溯源明细」抽屉 |
| 模型 evaluation + artifacts `st.json` | 验证与发布页 | L3 → 「模型证据」抽屉 |
| 原始报告 JSON | 报告页 | L3 → 「原始报告」抽屉 |
| 失败任务明细 | 「查看任务明细与失败重试」expander | L2 表格 + 行内重试按钮 |

## 附录 D：Streamlit 组件映射表（便于落地）

| 设计组件 | Streamlit 实现 |
|---|---|
| `card` / `card-inset` | `st.container(border=True)` + CSS 覆写，或 `st.markdown` 注入 HTML 片段 |
| `metric-tile` | `st.metric` + CSS 覆写 `[data-testid="stMetric"]` |
| `button-primary` | `st.button(type="primary")` / `st.form_submit_button(type="primary")` |
| `button-secondary` | `st.button()` / `st.form_submit_button()` |
| `table` | `st.dataframe` + `column_config`（数值列右对齐），或 `st.markdown` 渲染 HTML 表格以精确控制涨跌色 |
| `status-badge` | `st.markdown` + `unsafe_allow_html=True` 注入 span |
| `banner` | `st.warning` / `st.error` / `st.info`（已内建语义色，配合 CSS 覆写到本规范色板） |
| `detail-drawer` | **Streamlit 无原生抽屉**。降级方案：`st.dialog`（模态）或全宽 `st.expander`。若必须抽屉效果，需自定义组件或改用 HTML 覆盖层 |
| `command-palette` | `st.dialog` + `st.text_input` + 过滤列表；快捷键需前端 hack，可降级为顶栏搜索框 |
| `onboarding-step` | `st.markdown` 注入 HTML（含状态圆点与进度条） |
| 页签 | `st.tabs` |
| 侧边栏分组 | `st.sidebar` + `st.radio` + `micro` 组标题（`st.markdown` 注入） |
| Toast | `st.toast` |
| **`verdict-bar`（判决条）** | 无对应原生组件。`st.markdown` 注入 HTML；态别由 `blockers` 是否为空推导，**不得**用 `st.error`/`st.success` 替代——它们没有判决句与主行动区 |
| **`chain` / `gate`（解锁链路）** | `st.markdown` 注入 HTML（推荐，一次渲染整条链）；或 `st.container(border=True)` 逐节点渲染。**禁止**用 `st.warning` 逐条平铺 |
| **`fix-block`（卡点就地展开）** | `st.container(border=True)` + `st.button`；「需运维」类的指令用 `st.code`（自带复制按钮） |
| **`nav-dot`（侧边栏判决点）** | `st.sidebar` 内 HTML 注入；降级方案：`st.radio` 的选项文案前缀 `●`/`○` |
| **「我已处理，重新检测」** | `st.button` + `st.rerun()`；点击后 `st.spinner` 期间禁用，避免重复提交 |

**落地顺序建议**：

1. **文案全量中文化**（零风险、收益最大）——先消除「中英混杂」。
2. **统一状态语言**——六状态色板 + 三载体。
3. **判决面（依赖链 + 就地解锁）**——本附录 A.3/A.4 的规则；这一步才真正消灭往返。
4. **信息密度收敛**——L3 明细移入抽屉。
5. **效率增强与视觉精修**。

> 顺序说明：把「判决面」提到「信息密度收敛」之前。原因是信息密度收敛（做抽屉）是纯工作量，而判决面是一次**信息架构决策**——若先做抽屉再改 IA，抽屉的归属空间会全部返工。

## 附录 E：迭代路线图

| 阶段 | 内容 | 风险 | 收益 |
|---|---|---|---|
| **P0 文案中文化** | 7 个工作空间名称与描述、全部按钮/标签/表头/空状态改为中文；术语加问号气泡 | 极低（纯字符串） | 消除「中英混杂」这一最直观问题 |
| **P1 状态语言统一** | 六状态色板 + 三载体；顶栏收敛为一行徽标（**且徽标不得表达判决**） | 低 | 页面噪音大幅下降 |
| **P2 判决面** | 「今日」重构为判决条 + 解锁链路；`blockers` 平铺数组 → 依赖链（A.3）；卡点就地展开 + 动作类型三分（A.4）；`pipeline.py` 暴露结构化 blocker（带阶段与依赖字段）而非字符串数组 | 中高（需改后端 blocker 结构） | **消灭三个空间的一次往返**；「数据与任务」得以降级为只读 |
| **P3 信息密度收敛** | L3 明细全部移入抽屉；空状态补「下一步」操作 | 中（需新增抽屉组件） | 页面从"调试界面"变为"产品" |
| **P4 效率增强** | ⌘K 命令面板、表格列筛选、键盘导航 | 中 | 高频用户效率提升 |
| **P5 视觉精修** | 图表统一到设计令牌、动效细化、暗色主题评估 | 低 | 观感与品牌一致性 |

**每一阶段结束后必须更新本文件**，保持规范与实现同步。

**关于 P2 的实现前置**：`blockers` 目前是 `list[str]`，前端只能靠字符串匹配猜依赖关系——这是脆弱的。正确做法是让 `pipeline.py` 直接产出结构化结果（每个 blocker 带 `gate` / `stage` / `depends_on` / `action_type` 字段），判决面只做渲染。若这一步不做，A.3 的规则就只能在前端硬编码，后续新增门禁会立刻失真。

/**
 * 基础组件。刻意不引第三方 UI 库：这套设计系统的令牌、状态语义和密度都是定制的，
 * 引一个通用组件库反而要花更多力气去覆盖它的默认值。
 *
 * 一条贯穿全文件的可访问性约束：**状态不能只由颜色承载**。每个状态载体都同时带文字（和图标），
 * 色盲用户与黑白打印都要能读懂。
 */

import type { ButtonHTMLAttributes, ReactNode } from 'react';
import { useCallback, useEffect, useRef, useState } from 'react';

import type { Tone } from '@/domain/verdict';

/* ── 状态徽标 ─────────────────────────────────────────────────────────── */

interface BadgeProps {
  tone: Tone;
  children: ReactNode;
  /** 纯标签：去掉前置圆点，用于不需要「状态点」的场合。 */
  plain?: boolean;
  title?: string;
  /** 附加类名。顶栏用 `qp-badge--secondary` 标记「窄屏可隐藏」的次要事实。 */
  className?: string;
}

export function Badge({ tone, children, plain, title, className }: BadgeProps) {
  return (
    <span
      className={`qp-badge qp-${tone}${plain ? ' qp-badge--plain' : ''}${className ? ` ${className}` : ''}`}
      title={title}
    >
      {children}
    </span>
  );
}

/* ── 图标 ─────────────────────────────────────────────────────────────── */

const ICON_PATHS: Record<string, string> = {
  ok: 'M20 6 9 17l-5-5',
  warn: 'M12 9v4M12 17h.01M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z',
  error: 'M12 8v4M12 16h.01M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z',
  info: 'M12 16v-4M12 8h.01M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z',
  busy: 'M21 12a9 9 0 1 1-6.22-8.56',
  idle: 'M12 8v8M8 12h8M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z',
};

export function StatusIcon({ tone, className }: { tone: Tone; className?: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d={ICON_PATHS[tone] ?? ICON_PATHS.idle} />
    </svg>
  );
}

/* ── 横幅 ─────────────────────────────────────────────────────────────── */

interface BannerProps {
  tone: Tone;
  title: ReactNode;
  detail?: ReactNode;
  actions?: ReactNode;
}

export function Banner({ tone, title, detail, actions }: BannerProps) {
  return (
    <div className={`qp-banner qp-${tone}`} role={tone === 'error' ? 'alert' : 'status'}>
      <StatusIcon tone={tone} className="qp-banner__icon" />
      <div className="qp-banner__body">
        <div className="qp-banner__title">{title}</div>
        {detail ? <div className="qp-banner__detail">{detail}</div> : null}
        {actions ? <div className="qp-banner__actions">{actions}</div> : null}
      </div>
    </div>
  );
}

/* ── 按钮 ─────────────────────────────────────────────────────────────── */

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: 'default' | 'primary' | 'quiet';
  size?: 'md' | 'sm';
}

export function Button({ variant = 'default', size = 'md', className, ...rest }: ButtonProps) {
  const classes = [
    'qp-btn',
    variant === 'primary' ? 'qp-btn--primary' : '',
    variant === 'quiet' ? 'qp-btn--quiet' : '',
    size === 'sm' ? 'qp-btn--sm' : '',
    className ?? '',
  ]
    .filter(Boolean)
    .join(' ');
  return <button type="button" className={classes} {...rest} />;
}

/* ── 卡片 ─────────────────────────────────────────────────────────────── */

interface CardProps {
  title?: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
  flush?: boolean;
  children: ReactNode;
}

export function Card({ title, meta, actions, flush, children }: CardProps) {
  return (
    <section className={`qp-card${flush ? ' qp-card--flush' : ''}`}>
      {title || actions ? (
        <header className="qp-card__head" style={flush ? { padding: 'var(--space-4) var(--space-4) 0', marginBottom: 0 } : undefined}>
          <div>
            <div className="qp-card__title">{title}</div>
            {meta ? <div className="qp-card__meta">{meta}</div> : null}
          </div>
          {actions ? <div className="qp-row">{actions}</div> : null}
        </header>
      ) : null}
      <div style={flush && (title || actions) ? { padding: 'var(--space-3) 0 0' } : undefined}>{children}</div>
    </section>
  );
}

/* ── 空状态 ───────────────────────────────────────────────────────────── */

export function EmptyState({ children }: { children: ReactNode }) {
  return <div className="qp-empty">{children}</div>;
}

/* ── 可复制指令 ─────────────────────────────────────────────────────────
 * 需运维类的卡点只能给指令 + 复检。给一个点了不起作用的按钮，比不给按钮更伤信任（A.4）。 */

export function CopyableCommand({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    [],
  );

  const copy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(command);
    } catch {
      /* 非安全上下文下剪贴板不可用；指令本身可见可选，退化为手动复制。 */
    }
    setCopied(true);
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setCopied(false), 1600);
  }, [command]);

  return (
    <div className="qp-command">
      <code>{command}</code>
      <button type="button" onClick={copy} aria-label="复制这条指令">
        {copied ? '已复制' : '复制'}
      </button>
    </div>
  );
}

/* ── 表单字段 ─────────────────────────────────────────────────────────── */

interface FieldProps {
  label: string;
  hint?: ReactNode;
  error?: ReactNode;
  children: ReactNode;
  htmlFor?: string;
}

export function Field({ label, hint, error, children, htmlFor }: FieldProps) {
  return (
    <div className="qp-field">
      <label className="qp-field__label" htmlFor={htmlFor}>
        {label}
      </label>
      {children}
      {error ? <div className="qp-field__error">{error}</div> : null}
      {hint && !error ? <div className="qp-field__hint">{hint}</div> : null}
    </div>
  );
}

/* ── 骨架屏 ───────────────────────────────────────────────────────────── */

export function Skeleton({ width = '100%', height = 16 }: { width?: string | number; height?: number }) {
  return <div className="qp-skeleton" style={{ width, height }} aria-hidden="true" />;
}

export function LoadingBlock({ label = '正在读取…' }: { label?: string }) {
  return (
    <div className="qp-stack" role="status" aria-live="polite">
      <span className="qp-visually-hidden">{label}</span>
      <Skeleton height={14} />
      <Skeleton width="72%" height={14} />
      <Skeleton width="45%" height={14} />
    </div>
  );
}

/* ── 定义列表：判决面与详情页大量使用「标签 / 值」对 ─────────────────────── */

export function Facts({ items }: { items: { label: string; value: ReactNode }[] }) {
  return (
    <dl
      style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))',
        gap: 'var(--space-3)',
        margin: 0,
      }}
    >
      {items.map((item) => (
        <div key={item.label} style={{ minWidth: 0 }}>
          <dt className="qp-caption">{item.label}</dt>
          <dd style={{ margin: 0, fontWeight: 560, overflowWrap: 'anywhere' }}>{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}

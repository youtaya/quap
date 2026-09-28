/**
 * 轻提示。第三种状态载体：用于「刚才那个动作的结果」，短暂、不打断。
 *
 * 与横幅的分工：横幅说「这一屏为什么不工作」（常驻），轻提示说「你刚点的那个成没成」（一次性）。
 * 两者都不得用于表达判决——判决只在判决条上，全站唯一（顶栏徽标不表达判决是硬约束）。
 */

import { createContext, useCallback, useContext, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';

import { StatusIcon } from './ui';
import type { Tone } from '@/domain/verdict';

export interface Toast {
  id: number;
  tone: Tone;
  title: string;
  detail?: string;
}

interface ToastApi {
  push: (tone: Tone, title: string, detail?: string) => void;
  dismiss: (id: number) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

export function useToast(): ToastApi {
  const api = useContext(ToastContext);
  if (!api) throw new Error('useToast 必须在 ToastProvider 内部使用。');
  return api;
}

const LIFETIME_MS = 6000;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const nextId = useRef(1);
  const timers = useRef(new Map<number, number>());

  const dismiss = useCallback((id: number) => {
    setItems((current) => current.filter((item) => item.id !== id));
    const timer = timers.current.get(id);
    if (timer !== undefined) {
      window.clearTimeout(timer);
      timers.current.delete(id);
    }
  }, []);

  const push = useCallback(
    (tone: Tone, title: string, detail?: string) => {
      const id = nextId.current++;
      // 最多同时显示三条：再多就把内容区挡住了，而轻提示不该抢注意力。
      setItems((current) => [...current.slice(-2), { id, tone, title, ...(detail ? { detail } : {}) }]);
      const timer = window.setTimeout(() => dismiss(id), LIFETIME_MS);
      timers.current.set(id, timer);
    },
    [dismiss],
  );

  const api = useMemo(() => ({ push, dismiss }), [push, dismiss]);

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="qp-toasts" aria-live="polite" aria-atomic="false">
        {items.map((item) => (
          <div key={item.id} className={`qp-toast qp-${item.tone}`} role="status">
            <StatusIcon tone={item.tone} className="qp-banner__icon" />
            <div className="qp-toast__body">
              <div className="qp-toast__title">{item.title}</div>
              {item.detail ? <div className="qp-caption">{item.detail}</div> : null}
            </div>
            <button
              type="button"
              className="qp-toast__close"
              onClick={() => dismiss(item.id)}
              aria-label="关闭这条提示"
            >
              ×
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

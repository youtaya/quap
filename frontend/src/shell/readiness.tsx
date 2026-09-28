/**
 * 就绪数据的单一来源。
 *
 * 侧边栏的常驻判决块与「今日」的判决条读的是同一份数据，所以只取一次、放在这里共享。
 * 这也让「我已处理，重新检测」只有一个入口：刷新它，侧边栏和判决条同时更新——否则会出现
 * 判决条说「已通过」而侧边栏还挂着红色的自相矛盾。
 */

import { createContext, useContext, useMemo } from 'react';
import type { ReactNode } from 'react';

import { useApi } from '@/api/useApi';
import type { Resource } from '@/api/useApi';
import type { Readiness } from '@/api/types';

const ReadinessContext = createContext<Resource<Readiness> | null>(null);

export function ReadinessProvider({ children }: { children: ReactNode }) {
  const resource = useApi<Readiness>('/data-readiness');
  return <ReadinessContext.Provider value={resource}>{children}</ReadinessContext.Provider>;
}

export function useReadiness(): Resource<Readiness> {
  const resource = useContext(ReadinessContext);
  if (!resource) throw new Error('useReadiness 必须在 ReadinessProvider 内部使用。');
  return resource;
}

/** 侧边栏判决块的文案：短、只有「能不能」，细节留给判决条。 */
export interface MiniVerdict {
  tone: 'ok' | 'err' | 'idle';
  glyph: string;
  text: string;
}

export function useMiniVerdict(): MiniVerdict {
  const { data, error } = useReadiness();
  return useMemo(() => {
    if (error) return { tone: 'idle' as const, glyph: '·', text: '判决不可用' };
    if (!data) return { tone: 'idle' as const, glyph: '·', text: '正在读取…' };
    const day = data.frequencies?.day;
    if (!day) return { tone: 'idle' as const, glyph: '·', text: '判决不可用' };
    if (!day.ready) return { tone: 'err' as const, glyph: '✕', text: '受阻 · 出不了建议' };
    // `output` 是随结构化门禁链一起新增的字段。旧版 API 没有它，此时退化为「可出」而不是抛异常
    // ——侧边栏在每个页面上都渲染，它崩掉等于整站崩掉。
    if (!day.output?.published) return { tone: 'idle' as const, glyph: '◐', text: '可出 · 尚未生成' };
    return { tone: 'ok' as const, glyph: '✓', text: '建议已就绪' };
  }, [data, error]);
}

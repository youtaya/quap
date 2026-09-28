import { useCallback, useEffect, useRef, useState } from 'react';

import { ApiError, api } from './client';

export interface Resource<T> {
  data: T | null;
  error: ApiError | null;
  /** 首次加载中（还没有任何数据）。 */
  loading: boolean;
  /** 已有数据、正在重新拉取。用于让「重新检测」按钮进入 busy 态而不是把页面清空。 */
  refreshing: boolean;
  refresh: () => void;
}

/**
 * 读接口的取数钩子。**刻意不做自动轮询**——判决面的「我已处理，重新检测」必须由操作员确认后
 * 才重跑（附录 A.4）：自动轮询会让人以为平台自己恢复了，而真正恢复与否只有人知道。
 *
 * `key` 变化才重新取数；把它当成 useEffect 的依赖数组用。
 */
export function useApi<T>(path: string | null, key: readonly unknown[] = []): Resource<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [pending, setPending] = useState(path !== null);
  const [nonce, setNonce] = useState(0);
  const loaded = useRef(false);

  useEffect(() => {
    if (path === null) {
      setPending(false);
      return;
    }
    const controller = new AbortController();
    setPending(true);
    api
      .get<T>(path, controller.signal)
      .then((payload) => {
        setData(payload);
        setError(null);
        loaded.current = true;
      })
      .catch((failure: unknown) => {
        if (failure instanceof DOMException && failure.name === 'AbortError') return;
        setError(failure instanceof ApiError ? failure : new ApiError('offline', 0, '未知错误'));
      })
      .finally(() => {
        if (!controller.signal.aborted) setPending(false);
      });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, nonce, ...key]);

  const refresh = useCallback(() => setNonce((value) => value + 1), []);

  return {
    data,
    error,
    loading: pending && !loaded.current,
    refreshing: pending && loaded.current,
    refresh,
  };
}

export interface Action<A extends unknown[], R> {
  run: (...args: A) => Promise<R | null>;
  pending: boolean;
  error: ApiError | null;
  reset: () => void;
}

/**
 * 写接口的调用钩子。返回 `null` 表示失败——调用方只需要判断「成不成」，错误细节在 `error` 里，
 * 由横幅统一呈现，不必每个按钮自己写一遍错误 UI。
 */
export function useAction<A extends unknown[], R>(perform: (...args: A) => Promise<R>): Action<A, R> {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  const run = useCallback(
    async (...args: A) => {
      setPending(true);
      setError(null);
      try {
        return await perform(...args);
      } catch (failure) {
        setError(failure instanceof ApiError ? failure : new ApiError('offline', 0, '未知错误'));
        return null;
      } finally {
        setPending(false);
      }
    },
    [perform],
  );

  const reset = useCallback(() => setError(null), []);
  return { run, pending, error, reset };
}

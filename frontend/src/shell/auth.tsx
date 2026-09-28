/**
 * 登录状态。令牌存在 sessionStorage（见 `api/client.ts`），这里只负责让界面在
 * 「已登录 / 未登录」之间切换，以及在令牌失效（401）时把人送回登录页。
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';

import { readToken, writeToken } from '@/api/client';

interface AuthApi {
  token: string;
  signIn: (token: string) => void;
  signOut: () => void;
}

const AuthContext = createContext<AuthApi | null>(null);

export function useAuth(): AuthApi {
  const api = useContext(AuthContext);
  if (!api) throw new Error('useAuth 必须在 AuthProvider 内部使用。');
  return api;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string>(() => readToken());

  const signIn = useCallback((value: string) => {
    const trimmed = value.trim();
    writeToken(trimmed);
    setToken(trimmed);
  }, []);

  const signOut = useCallback(() => {
    writeToken('');
    setToken('');
  }, []);

  // 令牌失效时后端返回 401，客户端抛 `unauthorized`。这里监听一次，统一送回登录页，
  // 免得每个页面各自处理一遍「令牌过期」。
  useEffect(() => {
    const onUnauthorized = () => signOut();
    window.addEventListener('quap:unauthorized', onUnauthorized);
    return () => window.removeEventListener('quap:unauthorized', onUnauthorized);
  }, [signOut]);

  const api = useMemo(() => ({ token, signIn, signOut }), [token, signIn, signOut]);
  return <AuthContext.Provider value={api}>{children}</AuthContext.Provider>;
}

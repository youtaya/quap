/**
 * 登录页。它要回答的第一个问题不是「你的令牌是什么」，而是「后端到底在不在」——
 * 令牌错了和后端没起来是两种完全不同的处境，混成一句「登录失败」会让操作员查错方向。
 */

import { useEffect, useState } from 'react';

import { ApiError, api, probeLiveness } from '@/api/client';
import { Banner, Button, Field } from '@/components/ui';
import { useAuth } from './auth';

type Probe = 'checking' | 'up' | 'down';

export function Login() {
  const { signIn } = useAuth();
  const [token, setToken] = useState('');
  const [probe, setProbe] = useState<Probe>('checking');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    probeLiveness().then((alive) => {
      if (!cancelled) setProbe(alive ? 'up' : 'down');
    });
    return () => {
      cancelled = true;
    };
  }, []);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!token.trim()) {
      setError('请先粘贴访问令牌。');
      return;
    }
    setBusy(true);
    setError(null);
    // 用一次真实请求验证令牌：`/status` 需要鉴权，返回 200 就说明令牌是对的。
    const previous = sessionStorage.getItem('quap.token');
    sessionStorage.setItem('quap.token', token.trim());
    try {
      await api.get('/status');
      signIn(token);
    } catch (failure) {
      if (previous === null) sessionStorage.removeItem('quap.token');
      else sessionStorage.setItem('quap.token', previous);
      if (failure instanceof ApiError) {
        setError(
          failure.kind === 'unauthorized'
            ? '令牌不正确。请确认它来自本机的 deploy/secrets/api_token，且没有多余的空行。'
            : failure.kind === 'unconfigured'
              ? '后端没有配置访问令牌，拒绝所有请求。请先在部署侧生成 deploy/secrets/api_token。'
              : failure.detail,
        );
      } else {
        setError('未知错误。');
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="qp-login">
      <form className="qp-login__card" onSubmit={submit}>
        <div className="qp-login__brand">
          <div className="qp-brand__logo" aria-hidden="true">
            Q
          </div>
          <div>
            <div className="qp-brand__name">知衡量化</div>
            <div className="qp-brand__sub">QUAP 研究工作台</div>
          </div>
        </div>

        {probe === 'down' ? (
          <Banner
            tone="error"
            title="连不上后端"
            detail="前端本身是好的，但 API 进程没有响应。请先确认部署栈在运行，然后刷新本页。"
          />
        ) : null}

        <div className="qp-stack">
          <Field
            label="访问令牌"
            htmlFor="token"
            error={error}
            hint="本机 deploy/secrets/api_token 的内容。令牌只保存在这个浏览器标签页里，关掉即失效。"
          >
            <input
              id="token"
              className="qp-input qp-input--mono"
              type="password"
              autoComplete="off"
              spellCheck={false}
              value={token}
              onChange={(event) => setToken(event.target.value)}
              aria-invalid={error ? 'true' : undefined}
              placeholder="粘贴 64 位令牌"
            />
          </Field>

          <Button type="submit" variant="primary" disabled={busy || probe === 'down'}>
            {busy ? '正在验证…' : '进入工作台'}
          </Button>
        </div>

        <div className="qp-login__foot">
          本平台只做研究与建议，不执行交易。
          <br />
          所有写操作都会记入审计日志。
        </div>
      </form>
    </main>
  );
}

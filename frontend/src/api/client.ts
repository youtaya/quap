/**
 * 传输层。只做三件事：带上令牌、把后端的四种错误形状归一化、把网络失败与后端失败分开。
 *
 * 基地址刻意是**同源相对路径** `/api/v1`：开发期由 Vite 代理，生产期由前端容器反代到 api 服务。
 * 两条路径走同一份代码，所以前端里不存在「开发能跑、上线 404」这类差异，也不需要 CORS。
 */

const BASE = '/api/v1';

/** 令牌只放 sessionStorage，不放 localStorage：它是操作员凭据，不该在浏览器重启后仍然有效。 */
const TOKEN_KEY = 'quap.token';

export function readToken(): string {
  try {
    return sessionStorage.getItem(TOKEN_KEY) ?? '';
  } catch {
    return '';
  }
}

export function writeToken(token: string): void {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* 隐私模式下 sessionStorage 可能不可写；此时退化为「每次都要重新输入」，不影响功能。 */
  }
}

/**
 * 错误分类。前端**只**依据 `kind` 决定呈现，不去匹配 `detail` 的文本——后端改一句文案不该
 * 让界面换一种错误处理。
 */
export type ApiErrorKind =
  | 'unauthorized' // 401 令牌缺失或错误
  | 'unconfigured' // 503 服务端没有配置令牌
  | 'blocked' // 409 前置条件不满足（PipelineBlocked）
  | 'conflict' // 409 版本冲突，需重新读取后重试
  | 'invalid' // 422 请求体不合法
  | 'missing' // 404 资源不存在
  | 'unavailable' // 503 后端不可用
  | 'offline'; // 请求根本没到达后端

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status: number;
  readonly detail: string;

  constructor(kind: ApiErrorKind, status: number, detail: string) {
    super(detail);
    this.name = 'ApiError';
    this.kind = kind;
    this.status = status;
    this.detail = detail;
  }

  /** 这一类错误能不能靠「重新检测」解决。决定了横幅上出不出复检按钮。 */
  get retryable(): boolean {
    return this.kind !== 'invalid' && this.kind !== 'missing';
  }
}

function classify(status: number, payload: unknown): ApiErrorKind {
  const detail = typeof payload === 'object' && payload !== null ? (payload as Record<string, unknown>) : {};
  if (status === 401) return 'unauthorized';
  if (status === 404) return 'missing';
  if (status === 422) return 'invalid';
  if (status === 409) return detail.state === 'blocked' ? 'blocked' : 'conflict';
  if (status === 503) return detail.detail ? 'unconfigured' : 'unavailable';
  return 'unavailable';
}

function messageOf(status: number, payload: unknown): string {
  if (typeof payload === 'object' && payload !== null) {
    const detail = (payload as { detail?: unknown }).detail;
    if (typeof detail === 'string' && detail) return detail;
  }
  return `请求失败（HTTP ${status}）`;
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT';
  body?: unknown;
  /** 传 false 可跳过令牌——只有 `/health/live` 需要。 */
  auth?: boolean;
  signal?: AbortSignal;
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, auth = true, signal } = options;
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (auth) {
    const token = readToken();
    if (!token) throw new ApiError('unauthorized', 401, '尚未输入访问令牌。');
    headers.Authorization = `Bearer ${token}`;
  }
  if (body !== undefined) headers['Content-Type'] = 'application/json';

  let response: Response;
  try {
    response = await fetch(BASE + path, {
      method,
      headers,
      signal,
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new ApiError('offline', 0, '连不上后端。请确认 API 进程在运行，然后重新检测。');
  }

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }

  if (!response.ok) {
    const kind = classify(response.status, payload);
    // 令牌失效是全局事件：与其让每个页面各自处理一遍「401 之后怎么办」，不如在这里广播一次，
    // 由 `AuthProvider` 统一把人送回登录页。
    if (kind === 'unauthorized') {
      window.dispatchEvent(new CustomEvent('quap:unauthorized'));
    }
    throw new ApiError(kind, response.status, messageOf(response.status, payload));
  }
  return payload as T;
}

export const api = {
  get: <T>(path: string, signal?: AbortSignal) => request<T>(path, { signal }),
  post: <T>(path: string, body?: unknown) => request<T>(path, { method: 'POST', body }),
  put: <T>(path: string, body?: unknown) => request<T>(path, { method: 'PUT', body }),
};

/** 无令牌的存活探针，登录页用它区分「后端没起来」与「令牌错了」。 */
export async function probeLiveness(): Promise<boolean> {
  try {
    const response = await fetch('/health/live', { headers: { Accept: 'application/json' } });
    return response.ok;
  } catch {
    return false;
  }
}

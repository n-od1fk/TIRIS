export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}

let identity = localStorage.getItem('tiris-identity') || '';
const requests = new Set<AbortController>();
export function selectIdentity(id: string) {
  if (identity === id) return;
  requests.forEach(controller => controller.abort());
  identity = id;
  localStorage.setItem('tiris-identity', id);
}

export async function api<T>(path: string, init?: RequestInit, timeoutMs = 15000): Promise<T> {
  const controller = new AbortController();
  const selected = identity;
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const externalAbort = () => controller.abort();
  if (init?.signal?.aborted) controller.abort();
  init?.signal?.addEventListener('abort', externalAbort, { once: true });
  requests.add(controller);
  try {
  const response = await fetch(`/api${path}`, {
    ...init, headers: { 'Content-Type': 'application/json', ...(selected ? { 'X-TIRIS-User': selected } : {}), ...init?.headers },
    signal: controller.signal,
  });
  const data = await response.json();
  if (identity !== selected) throw new DOMException('身份已切换', 'AbortError');
  if (!response.ok) {
    const detail = data.detail;
    const message = typeof detail === 'string' ? detail : Array.isArray(detail) ?
      detail.map((e: { loc: string[]; msg: string }) => `${e.loc.join('.')}: ${e.msg}`).join('；') :
      detail?.message ?? `请求失败 (${response.status})`;
    throw new ApiError(message, response.status);
  }
  return data as T;
  } finally {
    clearTimeout(timer); requests.delete(controller);
    init?.signal?.removeEventListener('abort', externalAbort);
  }
}

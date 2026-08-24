// Central API client: base URL, JWT storage, and a fetch wrapper that attaches
// the access token and transparently refreshes it on a 401.

// In development Vite proxies /api/* → http://127.0.0.1:8000/* (see vite.config.ts),
// so we use a relative base to avoid CORS entirely. Override with VITE_API_BASE for
// production or when pointing at a remote backend.
export const API_BASE =
  (import.meta.env.VITE_API_BASE as string | undefined) ||
  (import.meta.env.DEV ? "/api" : "http://127.0.0.1:8000");

const ACCESS_KEY = "dbbuddy_access_token";
const REFRESH_KEY = "dbbuddy_refresh_token";

export function getAccessToken(): string | null {
  return localStorage.getItem(ACCESS_KEY);
}
export function getRefreshToken(): string | null {
  return localStorage.getItem(REFRESH_KEY);
}
export function setTokens(access: string, refresh: string): void {
  localStorage.setItem(ACCESS_KEY, access);
  localStorage.setItem(REFRESH_KEY, refresh);
}
export function clearTokens(): void {
  localStorage.removeItem(ACCESS_KEY);
  localStorage.removeItem(REFRESH_KEY);
}

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

// When the refresh token is also invalid/expired, the session is unrecoverable.
// The auth layer registers a handler here so it can drop the user → login screen.
let onAuthFailure: (() => void) | null = null;
export function setAuthFailureHandler(fn: (() => void) | null): void {
  onAuthFailure = fn;
}

// A 403 means authenticated-but-not-permitted (RBAC). The app registers a
// handler here to surface a single, consistent "you don't have access" toast
// instead of every call site inventing its own error copy.
let onForbidden: ((detail: string) => void) | null = null;
export function setForbiddenHandler(fn: ((detail: string) => void) | null): void {
  onForbidden = fn;
}

// Single-flight refresh so concurrent 401s don't trigger multiple refreshes.
let refreshInFlight: Promise<boolean> | null = null;

async function refreshAccess(): Promise<boolean> {
  if (refreshInFlight) return refreshInFlight;
  const refresh = getRefreshToken();
  if (!refresh) return false;
  refreshInFlight = (async () => {
    try {
      const res = await fetch(`${API_BASE}/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: refresh }),
      });
      if (!res.ok) {
        // Refresh token expired/invalid → unrecoverable; force logout.
        clearTokens();
        onAuthFailure?.();
        return false;
      }
      const data = await res.json();
      setTokens(data.access_token, data.refresh_token);
      return true;
    } catch {
      return false;
    } finally {
      refreshInFlight = null;
    }
  })();
  return refreshInFlight;
}

/** fetch() against the API with the bearer token attached and auto-refresh on 401. */
export async function apiFetch(
  path: string,
  options: RequestInit = {},
  _retry = true,
): Promise<Response> {
  const headers = new Headers(options.headers || {});
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const token = getAccessToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);

  const res = await fetch(`${API_BASE}${path}`, { ...options, headers });
  if (res.status === 401 && _retry && getRefreshToken()) {
    const ok = await refreshAccess();
    if (ok) return apiFetch(path, options, false);
  }
  return res;
}

/**
 * Read a Response body as JSON, tolerating an empty or non-JSON body.
 *
 * Callers that check `res.ok` themselves must not call `res.json()` blindly: an
 * empty body (a 502/504 from the Vite dev proxy when the backend is down, or any
 * bodyless error) makes `res.json()` throw "unexpected end of data at line 1
 * column 1", which then masks the real HTTP status. Returns `null` instead so the
 * caller can surface the actual failure (status / detail) rather than a parser
 * artifact.
 */
export async function readJson<T = unknown>(res: Response): Promise<T | null> {
  const text = await res.text();
  if (!text) return null;
  try {
    return JSON.parse(text) as T;
  } catch {
    return null;
  }
}

/** Like apiFetch but parses JSON and throws ApiError on non-2xx. Returns undefined for 204. */
export async function apiJson<T = unknown>(path: string, options: RequestInit = {}): Promise<T> {
  const res = await apiFetch(path, options);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json())?.detail || detail;
    } catch {
      /* non-JSON error body */
    }
    // Notify the global RBAC handler so the user gets one consistent toast,
    // while still throwing so the caller can handle/abort its own flow.
    if (res.status === 403) onForbidden?.(detail);
    throw new ApiError(detail, res.status);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// Central API client: base URL, JWT storage, and a fetch wrapper that attaches
// the access token and transparently refreshes it on a 401.

// In development Vite proxies /api/* → http://127.0.0.1:8000/* (see vite.config.ts),
// so we use a relative base to avoid CORS entirely. Override with VITE_API_BASE for
// production or when pointing at a remote backend.
export const API_BASE =
  (import.meta.env.VITE_API_BASE as string | undefined) ||
  (import.meta.env.DEV ? "/api" : "http://127.0.0.1:8000");

// Tokens are deliberately NOT in localStorage any more.
//
// Both used to be, which made any XSS anywhere in this app a full account
// takeover — and, because the refresh token was there too, one that outlived the
// 15-minute access token. The account can query connected business databases, so
// it is the highest-value thing this frontend holds.
//
// Now: the refresh token lives in an httpOnly cookie that script cannot read (the
// server sets it when we send `X-Auth-Mode: cookie`), and the access token lives
// in a module variable — gone on reload, restored by a refresh call. An XSS can
// still *use* the session while the page is open; it can no longer walk away with
// one that keeps working afterwards.
let accessToken: string | null = null;

export function getAccessToken(): string | null {
  return accessToken;
}

/**
 * Record a new session.
 *
 * The second argument is ignored and kept only so callers read naturally against
 * the API's `TokenPair`: in cookie mode the server sends an empty string there,
 * because the real refresh token went into the cookie.
 */
export function setTokens(access: string, _refresh?: string): void {
  accessToken = access;
}

export function clearTokens(): void {
  accessToken = null;
}

/** The double-submit CSRF token. Readable by design — echoing it is the check. */
function csrfToken(): string {
  const match = document.cookie.match(/(?:^|;\s*)dbbuddy_csrf=([^;]*)/);
  return match ? decodeURIComponent(match[1]) : "";
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

export async function refreshAccess(): Promise<boolean> {
  if (refreshInFlight) return refreshInFlight;
  // No early return on a missing token: the refresh credential is an httpOnly
  // cookie, so this code cannot see whether one exists. Asking the server is the
  // only way to find out, and a failed attempt costs one request.
  refreshInFlight = (async () => {
    try {
      const res = await fetch(`${API_BASE}/auth/refresh`, {
        method: "POST",
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
          "X-Auth-Mode": "cookie",
          "X-CSRF-Token": csrfToken(),
        },
        body: JSON.stringify({}),
      });
      if (!res.ok) {
        // Refresh token expired/invalid → unrecoverable; force logout.
        clearTokens();
        onAuthFailure?.();
        return false;
      }
      const data = await res.json();
      setTokens(data.access_token);
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
  // Declared on every request rather than only the auth ones: it is what tells
  // the server to put the refresh token in a cookie instead of the response body,
  // and the endpoints that do not issue sessions ignore it.
  headers.set("X-Auth-Mode", "cookie");

  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers,
    // Without this the browser withholds the refresh cookie on a cross-origin
    // call, and every reload would land on the login screen.
    credentials: "include",
  });
  // Retried unconditionally on a 401: whether a refresh cookie exists is not
  // knowable from script, so "do we have one?" can only be answered by trying.
  if (res.status === 401 && _retry) {
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

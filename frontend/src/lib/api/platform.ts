// Typed helpers for the platform (application-database) APIs.
import { apiJson } from "./client";

export type ApiConnection = {
  id: string;
  name: string;
  engine: string;
  host: string;
  port: number | null;
  username: string;
  database: string;
  created_at: string;
  // False when the stored password can no longer be decrypted (at-rest key
  // changed). The UI flags such a connection for password re-entry.
  credentials_ok: boolean;
};

export type ApiHistory = {
  id: string;
  nl_query: string;
  sql: string | null;
  status: "success" | "error";
  confidence: string | null;
  pinned: boolean;
  database_connection_id: string | null;
  created_at: string;
};

export type ApiChart = {
  id: string;
  title: string;
  nl_query: string | null;
  sql: string;
  chart_type: string;
  config: Record<string, unknown> | null;
  schema_fingerprint: string | null;
  status: string;
  database_connection_id: string | null;
  created_at: string;
  updated_at: string;
};

export const connectionsApi = {
  list: () => apiJson<ApiConnection[]>("/connections"),
  create: (body: {
    name: string;
    engine: string;
    host: string;
    port?: number | null;
    username: string;
    password: string;
    database: string;
  }) => apiJson<ApiConnection>("/connections", { method: "POST", body: JSON.stringify(body) }),
  update: (
    id: string,
    body: Partial<{
      name: string;
      engine: string;
      host: string;
      port: number | null;
      username: string;
      password: string;
      database: string;
    }>,
  ) =>
    apiJson<ApiConnection>(`/connections/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  remove: (id: string) => apiJson<void>(`/connections/${id}`, { method: "DELETE" }),
  removeAll: () => apiJson<void>("/connections", { method: "DELETE" }),
};

export const historyApi = {
  list: (connectionId?: string) =>
    apiJson<ApiHistory[]>(
      `/history${connectionId ? `?connection_id=${encodeURIComponent(connectionId)}` : ""}`,
    ),
  add: (body: {
    nl_query: string;
    sql?: string | null;
    status?: string;
    confidence?: string | null;
    pinned?: boolean;
    database_connection_id?: string | null;
  }) => apiJson<ApiHistory>("/history", { method: "POST", body: JSON.stringify(body) }),
  setPinned: (id: string, pinned: boolean) =>
    apiJson<ApiHistory>(`/history/${id}`, { method: "PATCH", body: JSON.stringify({ pinned }) }),
  remove: (id: string) => apiJson<void>(`/history/${id}`, { method: "DELETE" }),
  clear: (connectionId?: string) =>
    apiJson<void>(
      `/history${connectionId ? `?connection_id=${encodeURIComponent(connectionId)}` : ""}`,
      { method: "DELETE" },
    ),
};

export const chartsApi = {
  list: () => apiJson<ApiChart[]>("/charts"),
  create: (body: {
    title: string;
    nl_query?: string | null;
    sql: string;
    chart_type?: string;
    schema_fingerprint?: string | null;
    database_connection_id?: string | null;
    status?: string;
  }) => apiJson<ApiChart>("/charts", { method: "POST", body: JSON.stringify(body) }),
  update: (
    id: string,
    body: { title?: string; chart_type?: string; config?: Record<string, unknown> | null },
  ) => apiJson<ApiChart>(`/charts/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  remove: (id: string) => apiJson<void>(`/charts/${id}`, { method: "DELETE" }),
  removeAll: () => apiJson<void>("/charts", { method: "DELETE" }),
  publish: (id: string, visibility: "organization" | "private" = "organization") =>
    apiJson<ApiPublication>(`/charts/${id}/publish`, {
      method: "POST",
      body: JSON.stringify({ visibility }),
    }),
  unpublish: (id: string) => apiJson<void>(`/charts/${id}/unpublish`, { method: "POST" }),
};

export type ApiPublication = {
  id: string;
  chart_id: string;
  status: string;
  visibility: string;
  published_at: string;
};

// ── Client-facing published reports (Milestone 3) ────────────────────────────

export type ApiReport = {
  id: string;
  chart_id: string;
  title: string;
  chart_type: string;
  nl_query: string | null;
  visibility: string;
  published_at: string;
};

export type ReportRun = {
  ok: boolean;
  columns: string[];
  rows: Record<string, unknown>[];
  needs_attention: boolean;
  message: string | null;
  chart_type: string;
  config: Record<string, unknown> | null;
};

export const reportsApi = {
  list: () => apiJson<ApiReport[]>("/reports"),
  get: (id: string) => apiJson<ApiReport>(`/reports/${id}`),
  run: (id: string) => apiJson<ReportRun>(`/reports/${id}/run`, { method: "POST" }),
};

// ── Organizations & user administration (Milestone 2) ────────────────────────

export type ApiOrg = {
  id: string;
  name: string;
  slug: string;
  is_default: boolean;
  member_count: number;
  created_at: string;
};

export type AdminUser = {
  id: string;
  email: string;
  full_name: string | null;
  is_active: boolean;
  organization_id: string;
  roles: string[];
  created_at: string;
};

export const orgsApi = {
  list: () => apiJson<ApiOrg[]>("/orgs"),
  create: (name: string) =>
    apiJson<ApiOrg>("/orgs", { method: "POST", body: JSON.stringify({ name }) }),
  rename: (id: string, name: string) =>
    apiJson<ApiOrg>(`/orgs/${id}`, { method: "PATCH", body: JSON.stringify({ name }) }),
  remove: (id: string) => apiJson<void>(`/orgs/${id}`, { method: "DELETE" }),
};

export const adminUsersApi = {
  list: () => apiJson<AdminUser[]>("/admin/users"),
  create: (body: {
    email: string;
    password: string;
    full_name?: string | null;
    role: string;
    organization_id?: string | null;
  }) => apiJson<AdminUser>("/admin/users", { method: "POST", body: JSON.stringify(body) }),
  update: (id: string, body: { roles?: string[]; is_active?: boolean; organization_id?: string }) =>
    apiJson<AdminUser>(`/admin/users/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  remove: (id: string) => apiJson<void>(`/admin/users/${id}`, { method: "DELETE" }),
};

// ── Audit dashboard (Milestone 4) ────────────────────────────────────────────

export type AuditEntry = {
  id: string;
  user_id: string | null;
  actor_email: string | null;
  organization_id: string | null;
  entity_type: string | null;
  action: string;
  entity_id: string | null;
  detail: Record<string, unknown> | null;
  ip_address: string | null;
  created_at: string;
};

export type AuditSummary = {
  window_days: number;
  total: number;
  by_action: Record<string, number>;
  by_entity: Record<string, number>;
  active_users: number;
  recent_failures: number;
};

export type AuditFilters = {
  entity_type?: string;
  action?: string;
  user_id?: string;
  created_from?: string;
  created_to?: string;
  limit?: number;
  offset?: number;
};

// ── MFA / 2FA (Milestone 5) ──────────────────────────────────────────────────

export type MfaSetup = { secret: string; otpauth_uri: string; qr_svg: string };
export type MfaEnableResult = { enabled: boolean; recovery_codes: string[] };

export const mfaApi = {
  setup: () => apiJson<MfaSetup>("/auth/mfa/setup", { method: "POST" }),
  verify: (code: string) =>
    apiJson<MfaEnableResult>("/auth/mfa/verify", {
      method: "POST",
      body: JSON.stringify({ code }),
    }),
  disable: (body: { password?: string; code?: string }) =>
    apiJson<{ ok: boolean }>("/auth/mfa/disable", { method: "POST", body: JSON.stringify(body) }),
};

// ── Personal API keys (CLI / automation) ────────────────────────────────────

export type ApiKey = {
  id: string;
  name: string;
  token_prefix: string;
  last_used_at: string | null;
  revoked_at: string | null;
  created_at: string;
};

// Returned only at creation — carries the raw secret, shown exactly once.
export type ApiKeyCreated = ApiKey & { api_key: string };

export const apiKeysApi = {
  list: () => apiJson<ApiKey[]>("/auth/keys"),
  create: (name: string) =>
    apiJson<ApiKeyCreated>("/auth/keys", { method: "POST", body: JSON.stringify({ name }) }),
  rename: (id: string, name: string) =>
    apiJson<ApiKey>(`/auth/keys/${id}`, { method: "PATCH", body: JSON.stringify({ name }) }),
  revoke: (id: string) => apiJson<void>(`/auth/keys/${id}`, { method: "DELETE" }),
};

// ── Background jobs & notifications (Milestone 6) ────────────────────────────

export type ApiJob = {
  id: string;
  name: string;
  job_type: "report_refresh" | "context_rebuild";
  target_ref: string | null;
  schedule_kind: "interval" | "daily" | "weekly" | "manual";
  schedule_config: Record<string, unknown> | null;
  enabled: boolean;
  last_run_at: string | null;
  last_status: string | null;
  last_error: string | null;
  next_run_at: string | null;
  created_at: string;
  updated_at: string;
};

export type ApiJobRun = {
  id: string;
  job_id: string;
  status: string;
  message: string | null;
  started_at: string;
  finished_at: string | null;
};

export const jobsApi = {
  list: () => apiJson<ApiJob[]>("/jobs"),
  create: (body: {
    name: string;
    job_type: string;
    target_ref: string;
    schedule_kind: string;
    schedule_config?: Record<string, unknown> | null;
    enabled?: boolean;
  }) => apiJson<ApiJob>("/jobs", { method: "POST", body: JSON.stringify(body) }),
  update: (
    id: string,
    body: Partial<{
      name: string;
      schedule_kind: string;
      schedule_config: Record<string, unknown>;
      enabled: boolean;
    }>,
  ) => apiJson<ApiJob>(`/jobs/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  remove: (id: string) => apiJson<void>(`/jobs/${id}`, { method: "DELETE" }),
  run: (id: string) => apiJson<ApiJobRun>(`/jobs/${id}/run`, { method: "POST" }),
  runs: (id: string) => apiJson<ApiJobRun[]>(`/jobs/${id}/runs`),
};

export type ApiNotification = {
  id: string;
  title: string;
  body: string | null;
  level: "info" | "success" | "error";
  read: boolean;
  created_at: string;
};

export const notificationsApi = {
  list: () => apiJson<ApiNotification[]>("/notifications"),
  unreadCount: () => apiJson<{ count: number }>("/notifications/unread-count"),
  markRead: (id: string) => apiJson<void>(`/notifications/${id}/read`, { method: "POST" }),
  markAllRead: () => apiJson<void>("/notifications/read-all", { method: "POST" }),
};

// ── Unified AI provider management ───────────────────────────────────────────

export type AIAdapter = "openai_compatible" | "ollama";

export type AIProvider = {
  id: string;
  name: string;
  adapter: AIAdapter;
  base_url: string | null;
  model: string;
  enabled: boolean;
  priority: number;
  is_active: boolean; // priority === 1 (the org's default)
  has_key: boolean; // a key is stored (value never returned)
  credentials_ok: boolean; // false when the stored key no longer decrypts
  fallback_provider_id: string | null;
  created_at: string;
  updated_at: string;
};

export type AIProviderTestResult = {
  ok: boolean;
  adapter: string;
  model: string;
  error: string | null;
};

export type AIProviderInput = {
  name: string;
  adapter: AIAdapter;
  base_url?: string | null;
  model: string;
  api_key?: string | null; // blank/omitted on edit keeps the stored key
  enabled?: boolean;
  fallback_provider_id?: string | null;
};

// Per-provider observability, keyed by provider name (see /ai-metrics).
export type AIProviderMetric = {
  name: string;
  adapter: string;
  calls: number;
  successes: number;
  failovers: number;
  retries: number;
  rate_limited: number;
  skipped: number;
  avg_latency_ms: number;
  last_latency_ms: number;
  last_outcome: string | null;
  last_ts: number | null;
  circuit_state: "closed" | "open" | "half_open";
  circuit_cooldown_s: number;
};

export const aiProvidersApi = {
  list: () => apiJson<AIProvider[]>("/ai-providers"),
  create: (body: AIProviderInput) =>
    apiJson<AIProvider>("/ai-providers", { method: "POST", body: JSON.stringify(body) }),
  update: (id: string, body: Partial<AIProviderInput>) =>
    apiJson<AIProvider>(`/ai-providers/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  remove: (id: string) => apiJson<void>(`/ai-providers/${id}`, { method: "DELETE" }),
  activate: (id: string) => apiJson<AIProvider>(`/ai-providers/${id}/activate`, { method: "POST" }),
  duplicate: (id: string) =>
    apiJson<AIProvider>(`/ai-providers/${id}/duplicate`, { method: "POST" }),
  test: (id: string) =>
    apiJson<AIProviderTestResult>(`/ai-providers/${id}/test`, { method: "POST" }),
  metrics: () => apiJson<{ providers: AIProviderMetric[] }>("/ai-metrics"),
};

export const auditApi = {
  list: (f: AuditFilters = {}) => {
    const qs = new URLSearchParams();
    Object.entries(f).forEach(([k, v]) => {
      if (v !== undefined && v !== "") qs.set(k, String(v));
    });
    const q = qs.toString();
    return apiJson<AuditEntry[]>(`/admin/audit${q ? `?${q}` : ""}`);
  },
  summary: (windowDays = 7) =>
    apiJson<AuditSummary>(`/admin/audit/summary?window_days=${windowDays}`),
};

// ── Relation graph (Infographics → Relations; analyst-only) ───────────────────

export type RelationOverviewNode = {
  id: string;
  label: string;
  engine: string | null;
  database: string | null;
  table_count: number;
  has_snapshot: boolean;
  captured_at: string | null;
};

export type RelationOverviewEdge = {
  source: string;
  target: string;
  confidence: number;
  shared: string[];
  shared_count: number;
  kind: "inferred";
};

export type RelationOverview = {
  level: "overview";
  nodes: RelationOverviewNode[];
  edges: RelationOverviewEdge[];
  stats: {
    databases: number;
    snapshotted: number;
    links: number; // links present in this payload
    total_links: number; // links that exist; equals `links` unless `truncated`
    truncated: boolean;
  };
};

export type RelationDetailNode = {
  id: string;
  label: string;
  column_count: number;
  primary_keys: string[];
};

export type RelationDetailEdge = {
  source: string;
  target: string;
  from_col: string;
  to_col: string;
  kind: "fk" | "heuristic";
};

export type RelationDetail = {
  level: "detail";
  connection_id: string;
  label: string;
  nodes: RelationDetailNode[];
  edges: RelationDetailEdge[];
  stats: { tables: number; relationships: number };
};

export type SnapshotResult = { connection_id: string; table_count: number; captured_at: string };
export type SnapshotAllResult = {
  captured: { connection_id: string; table_count: number }[];
  failed: { connection_id: string; name: string; error: string }[];
};

export const relationsApi = {
  overview: () => apiJson<RelationOverview>("/relations/overview"),
  detail: (connectionId: string) => apiJson<RelationDetail>(`/relations/detail/${connectionId}`),
  snapshot: (connectionId: string) =>
    apiJson<SnapshotResult>(`/relations/snapshot/${connectionId}`, { method: "POST" }),
  snapshotAll: () => apiJson<SnapshotAllResult>("/relations/snapshot", { method: "POST" }),
};

// ── Insights Engine (analyst-only) ───────────────────────────────────────────
// Explanations of an *executed* result. The rows are posted from what the client
// already has rather than re-running the query: the deterministic engine has
// already executed it, and re-executing to explain it would double the load on
// the customer database and risk explaining data the user is not looking at.

export type InsightFinding = {
  title: string;
  detail: string;
  evidence: string;
  // Tapered below 1 when the model hedged — the finding is kept and ranked
  // lower rather than dropped.
  confidence: number;
};

export type InsightBundle = {
  summary: string;
  findings: InsightFinding[];
  recommendations: string[];
  limitations: string[];
  provider: string | null;
  prompt_version: string;
  cached: boolean;
};

export type InsightResponse = {
  insights: InsightBundle;
  markdown: string;
  suggested_questions: string[];
  row_count: number;
};

export type InsightAnswer = {
  answer: string;
  evidence: string;
  limitations: string[];
  provider: string | null;
  prompt_version: string;
};

export type InsightsSettings = {
  enabled: boolean;
  configured: boolean;
  provider: string | null;
  prompt_version: string;
  temperature: number;
  max_sample_rows: number;
  max_rows: number;
};

export type InsightsTurn = { role: "user" | "assistant"; content: string };

export type InsightRequestBody = {
  connection_id?: string | null;
  question?: string;
  sql?: string;
  database?: string;
  rows: Record<string, unknown>[];
  chart_type?: string | null;
  regenerate?: boolean;
};

// ── Dashboards (collections of charts + narrative) ───────────────────────────
// A dashboard holds no data: opening one re-runs every pinned chart live. Each
// chart carries its own ok/needs_attention and its own fetched_at, so one broken
// chart never blanks the rest and the UI can state how current the data is.

export type DashboardItem = {
  id: string;
  chart_id: string;
  title: string;
  chart_type: string;
  nl_query: string | null;
  description: string | null;
  description_source: "manual" | "ai" | null;
  position: number;
};

export type ApiDashboard = {
  id: string;
  title: string;
  description: string | null;
  status: string;
  created_at: string;
  updated_at: string;
  items: DashboardItem[];
};

export type DashboardChartRun = {
  chart_id: string;
  item_id: string;
  title: string;
  description: string | null;
  description_source: "manual" | "ai" | null;
  ok: boolean;
  columns: string[];
  rows: Record<string, unknown>[];
  needs_attention: boolean;
  message: string | null;
  chart_type: string;
  config: Record<string, unknown> | null;
  /** When this chart's data was read. A cached result reports its original time. */
  fetched_at: string | null;
  cached: boolean;
  /** Rows the query produced, which may exceed rows.length when truncated. */
  row_count: number;
  truncated: boolean;
};

export type DashboardRun = {
  id: string;
  title: string;
  description: string | null;
  charts: DashboardChartRun[];
  cache_ttl_seconds: number;
};

export type DashboardDescription = {
  description: string;
  provider: string | null;
  limitations: string[];
};

export const dashboardsApi = {
  list: () => apiJson<ApiDashboard[]>("/dashboards"),
  get: (id: string) => apiJson<ApiDashboard>(`/dashboards/${id}`),
  create: (body: { title: string; description?: string | null }) =>
    apiJson<ApiDashboard>("/dashboards", { method: "POST", body: JSON.stringify(body) }),
  update: (id: string, body: { title?: string; description?: string | null }) =>
    apiJson<ApiDashboard>(`/dashboards/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  remove: (id: string) => apiJson<void>(`/dashboards/${id}`, { method: "DELETE" }),

  pin: (id: string, body: { chart_id: string; description?: string | null }) =>
    apiJson<ApiDashboard>(`/dashboards/${id}/items`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateItem: (id: string, itemId: string, body: { description: string | null }) =>
    apiJson<ApiDashboard>(`/dashboards/${id}/items/${itemId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  unpin: (id: string, itemId: string) =>
    apiJson<ApiDashboard>(`/dashboards/${id}/items/${itemId}`, { method: "DELETE" }),
  reorder: (id: string, itemIds: string[]) =>
    apiJson<ApiDashboard>(`/dashboards/${id}/reorder`, {
      method: "POST",
      body: JSON.stringify({ item_ids: itemIds }),
    }),

  /** Draft a description with the Insights Engine. Returns it; does not save. */
  describe: (id: string, itemId: string, refresh = false) =>
    apiJson<DashboardDescription>(`/dashboards/${id}/items/${itemId}/describe`, {
      method: "POST",
      body: JSON.stringify({ refresh }),
    }),
  acceptDescription: (id: string, itemId: string, description: string) =>
    apiJson<ApiDashboard>(`/dashboards/${id}/items/${itemId}/describe/accept`, {
      method: "POST",
      body: JSON.stringify({ description }),
    }),

  publish: (id: string) => apiJson<ApiDashboard>(`/dashboards/${id}/publish`, { method: "POST" }),
  unpublish: (id: string) =>
    apiJson<ApiDashboard>(`/dashboards/${id}/unpublish`, { method: "POST" }),

  published: () => apiJson<ApiDashboard[]>("/dashboards/published/list"),
  run: (id: string, refresh = false) =>
    apiJson<DashboardRun>(`/dashboards/${id}/run?refresh=${refresh}`, { method: "POST" }),
};

export const insightsApi = {
  settings: () => apiJson<InsightsSettings>("/insights/settings"),
  generate: (body: InsightRequestBody) =>
    apiJson<InsightResponse>("/insights/generate", { method: "POST", body: JSON.stringify(body) }),
  ask: (body: InsightRequestBody & { followup: string; history?: InsightsTurn[] }) =>
    apiJson<InsightAnswer>("/insights/ask", { method: "POST", body: JSON.stringify(body) }),
};

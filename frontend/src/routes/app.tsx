import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  MessageSquare,
  History,
  Database,
  Settings,
  Plus,
  Send,
  Copy,
  Bookmark,
  Check,
  ChevronDown,
  ChevronRight,
  Sparkles,
  Table as TableIcon,
  BarChart3,
  Braces,
  Sun,
  Moon,
  Clock,
  Rows,
  CircleDot,
  AlertTriangle,
  Loader2,
  AlertCircle,
  X,
  RefreshCw,
  Pencil,
  ArrowRight,
  Download,
  Search,
  Columns,
  ArrowLeftRight,
  CheckCircle2,
  Zap,
  Trash2,
  ChevronLeft,
  LayoutDashboard,
  LineChart,
  Network,
  LogOut,
  Palette,
} from "lucide-react";
import { Toaster } from "@/components/ui/sonner";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { Collapsible, CollapsibleTrigger, CollapsibleContent } from "@/components/ui/collapsible";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from "recharts";
import {
  ChartRenderer,
  CHART_TYPES,
  PALETTES,
  colorTargets,
  type ChartType,
  type ChartConfig,
} from "@/components/ChartRenderer";
import { cn } from "@/lib/utils";
import { SemanticGroup, type SemanticColumn } from "@/components/SemanticGroup";
import { AuthProvider, useAuth } from "@/lib/auth";
import { AuthScreen } from "@/components/AuthScreen";
import { RoleLanding } from "@/components/RoleLanding";
import { AdminConsole } from "@/components/AdminConsole";
import { ClientReports } from "@/components/ClientReports";
import { MfaCard } from "@/components/MfaCard";
import { PersonalApiKeys } from "@/components/PersonalApiKeys";
import { SchedulesPanel } from "@/components/SchedulesPanel";
import { RelationGraph } from "@/components/RelationGraph";
import { InsightsPanel } from "@/components/InsightsPanel";
import { DashboardsPanel } from "@/components/DashboardsPanel";
import { PinToDashboard } from "@/components/PinToDashboard";
import { NotificationsBell } from "@/components/NotificationsBell";
import { AIProviders } from "@/components/AIProviders";
import { apiFetch, apiJson, readJson, API_BASE, setForbiddenHandler } from "@/lib/api/client";
import {
  connectionsApi,
  historyApi,
  chartsApi,
  type ApiConnection,
  type ApiHistory,
  type ApiChart,
} from "@/lib/api/platform";

export const Route = createFileRoute("/app")({
  component: AppShell,
});

// Render nothing on the server — the app is fully client-driven with no SSR
// value. This eliminates all hydration mismatches from state-dependent props
// (disabled, placeholder, title) that differ between server and client.
function AppShell() {
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    setMounted(true);
  }, []);
  if (!mounted) return <AppLoadingSkeleton />;
  return (
    <AuthProvider>
      <AuthGate />
    </AuthProvider>
  );
}

// Gate the app behind authentication AND role: Role → Landing → correct
// workspace. While the session restores, show the skeleton; unauthenticated
// users get the login screen. Routing precedence:
//   • `query:run`   → the full DB Buddy Analyst workspace
//   • `user:manage` → the Admin console (platform admin / org admin) [M2]
//   • `report:view` → the read-only client Reports viewer [M3]
//   • otherwise     → RoleLanding (no workspace granted yet)
function AuthGate() {
  const { user, loading } = useAuth();
  if (loading) return <AppLoadingSkeleton />;
  if (!user) return <AuthScreen />;
  if (user.permissions.includes("query:run")) return <DBBuddyApp />;
  if (user.permissions.includes("user:manage")) return <AdminConsole />;
  if (user.permissions.includes("report:view")) return <ClientReports />;
  return <RoleLanding />;
}

// Matches the app's two-panel layout so there's no layout shift on mount.
function AppLoadingSkeleton() {
  return (
    <div className="flex h-screen w-full overflow-hidden bg-background">
      {/* Sidebar skeleton */}
      <aside className="hidden w-72 shrink-0 flex-col gap-4 border-r border-border bg-sidebar px-5 py-5 md:flex">
        <div className="flex items-center gap-3">
          <div className="h-9 w-9 rounded-xl bg-card animate-pulse" />
          <div className="flex flex-col gap-1.5">
            <div className="h-3.5 w-24 rounded bg-card animate-pulse" />
            <div className="h-2.5 w-32 rounded bg-card animate-pulse" />
          </div>
        </div>
        <div className="h-9 w-full rounded-lg bg-card animate-pulse" />
        <div className="flex flex-col gap-2 mt-2">
          {[1, 2, 3, 4].map((i) => (
            <div key={i} className="h-8 w-full rounded-lg bg-card animate-pulse" />
          ))}
        </div>
      </aside>
      {/* Main area skeleton */}
      <main className="flex flex-1 flex-col">
        <div className="flex items-center justify-between border-b border-border px-8 py-3">
          <div className="flex flex-col gap-1.5">
            <div className="h-4 w-48 rounded bg-card animate-pulse" />
            <div className="h-3 w-32 rounded bg-card animate-pulse" />
          </div>
          <div className="flex gap-2">
            <div className="h-8 w-24 rounded-md bg-card animate-pulse" />
            <div className="h-8 w-32 rounded-md bg-card animate-pulse" />
          </div>
        </div>
        <div className="flex flex-1 items-center justify-center">
          <div className="flex flex-col items-center gap-4">
            <div className="h-16 w-16 rounded-2xl bg-card animate-pulse" />
            <div className="h-4 w-48 rounded bg-card animate-pulse" />
            <div className="h-3 w-64 rounded bg-card animate-pulse" />
          </div>
        </div>
        <div className="border-t border-border px-8 py-4">
          <div className="h-14 w-full rounded-2xl bg-card animate-pulse" />
        </div>
      </main>
    </div>
  );
}

// ---------- Types ----------
type DBStatus = "connected" | "disconnected";
// A connected database. `id` is the backend connection id; queries reference it
// (the backend resolves and decrypts credentials), so the password is never
// held client-side after the connection is created.
type Section = "chat" | "history" | "infographics" | "schedules" | "settings";

type DB = {
  id: string;
  name: string;
  engine: string;
  status: DBStatus;
  host: string;
  user: string;
  database: string;
  // False when the backend can no longer decrypt this connection's saved
  // password (at-rest key changed). Surfaced as a warning + edit prompt.
  credentialsOk: boolean;
};

// A persisted query-history entry, scoped to one database.
// `pinned` items are explicitly saved by the user (via the Generated SQL "Save"
// button): they survive the history cap and "Clear history", and only go away
// when the user deletes them individually.
type HistoryItem = {
  id: string;
  query: string;
  status: "success" | "error";
  confidence?: "high" | "medium" | "low";
  sql?: string;
  pinned?: boolean;
  ts: number;
};

// Map a backend history row to the frontend item shape.
function apiHistoryToItem(h: ApiHistory): HistoryItem {
  return {
    id: h.id,
    query: h.nl_query,
    status: h.status,
    confidence: (h.confidence as HistoryItem["confidence"]) ?? undefined,
    sql: h.sql ?? undefined,
    pinned: h.pinned,
    ts: Date.parse(h.created_at) || Date.now(),
  };
}

// A saved chart ("infographic"). Only the configuration is stored — never the
// data — so opening it always re-runs the SQL against the live database.
// Charts now live in the backend, scoped to the account; `databaseConnectionId`
// links to the connection used to refresh it.
type SavedChart = {
  id: string;
  title: string;
  nlQuery?: string;
  sql: string;
  chartType: ChartType;
  config: ChartConfig | null;
  databaseConnectionId: string | null;
  schemaFingerprint?: string;
  status: "draft" | "published";
  createdAt: number;
};

// Map a backend chart row to the frontend shape.
function apiChartToSaved(c: ApiChart): SavedChart {
  return {
    id: c.id,
    title: c.title,
    nlQuery: c.nl_query ?? undefined,
    sql: c.sql,
    chartType: (c.chart_type as ChartType) || "column",
    config: (c.config as ChartConfig | null) ?? null,
    databaseConnectionId: c.database_connection_id,
    schemaFingerprint: c.schema_fingerprint ?? undefined,
    status: c.status === "published" ? "published" : "draft",
    createdAt: Date.parse(c.created_at) || Date.now(),
  };
}

// Stable signature of a result's columns — used to detect that the underlying
// schema changed since a chart was saved (columns added/removed/renamed).
function columnFingerprint(columns: string[]): string {
  return [...columns].sort().join("|");
}

// localStorage keys. The full DB list and per-database query history are
// persisted so they survive a reload (the connection is still "there"); they
// are only removed when the user explicitly disconnects or clears history.
const DB_LIST_KEY = "dbbuddy_databases";
const HISTORY_KEY = "dbbuddy_query_history";
const INFOGRAPHICS_KEY = "dbbuddy_infographics"; // saved chart configs
const LEGACY_DB_CONFIG_KEY = "db_config"; // single-DB format, migrated on load

// Stable identity for a database, independent of its display name or the random
// id assigned at runtime — so history follows the actual host/database/engine.
function dbKey(db: { host: string; database: string; engine: string }): string {
  return `${db.host}|${db.database}|${db.engine.toLowerCase()}`;
}

// Pretty display label for an engine identifier ("mysql" → "MySQL").
function prettyEngine(engine: string): string {
  const m: Record<string, string> = {
    mysql: "MySQL",
    postgresql: "PostgreSQL",
    postgres: "PostgreSQL",
    sqlserver: "SQL Server",
    mssql: "SQL Server",
    "sql server": "SQL Server",
  };
  return m[engine.toLowerCase()] ?? engine;
}

// Map a backend connection record to the frontend DB shape (id = connection id).
function apiConnToDb(c: ApiConnection): DB {
  return {
    id: c.id,
    name: c.name,
    engine: prettyEngine(c.engine),
    status: "connected",
    host: c.host,
    user: c.username,
    database: c.database,
    // Default true for older responses that predate the field.
    credentialsOk: c.credentials_ok !== false,
  };
}

type SemanticAnalysisResult = {
  semantic_layer: Record<
    string,
    Record<string, { term: string; source?: string; provider?: string; plugin?: string }>
  >;
  metadata: {
    database: string;
    ai_used: boolean;
    ai_requested?: boolean;
    ai_provider?: string | null;
    ai_providers_used?: string[];
    ai_error?: string | null;
  };
};

type QueryResult = {
  columns: string[];
  rows: (string | number)[][];
  timeMs: number;
  source: string;
  semantic: { from: string; to: string }[];
  termInterpretations?: { term: string; mapped_to: string; type: string }[];
  relevanceReason?: string;
};

/** The `/query` response, typed from what this component reads rather than from
 *  the full backend payload. Every field is optional: an error response, a
 *  pending-confirmation response, and an executed read each carry a different
 *  subset, and the reader already handles all three. */
type QueryApiResponse = {
  detail?: string;
  error?: string;
  sql?: string;
  confidence?: "high" | "medium" | "low";
  warning?: string;
  query_type?: string;
  requires_confirmation?: boolean;
  auto_executed?: boolean;
  auto_fixed?: boolean;
  execution_token?: string;
  term_interpretations?: QueryResult["termInterpretations"];
  relevance_check?: { reason?: string };
  results?: Record<string, unknown>[];
  /** Only the slice of columns this query touched — see `ai_labeled`. */
  semantic_layer?: Record<string, Record<string, { term: string; source?: string }>>;
  ai_labeled?: boolean;
  explanation?: Explanation;
  meta?: {
    request_id?: string;
    latency_ms?: number;
    stage_timings?: Record<string, number>;
    compiler_version?: string;
    schema_hash?: string;
  };
};

/** The `/execute` response. A write reports `write` + `rows_affected`; a read
 *  returns `results`. */
type ExecuteApiResponse = {
  detail?: string;
  error?: string;
  write?: boolean;
  message?: string;
  rows_affected?: number;
  results?: Record<string, unknown>[];
};

type Explanation = {
  summary: string;
  interpretation: {
    metric: string | null;
    aggregation: string | null;
    grouping: string[];
  };
  memory_usage: Array<{ term: string; mapped_to: string; source: string; used: boolean }>;
  reasoning: string[];
};

type Message =
  | { id: string; role: "user"; text: string }
  | {
      id: string;
      role: "assistant";
      text: string;
      headline?: string;
      sql: string;
      status: "success" | "error" | "loading" | "pending_confirmation";
      result?: QueryResult;
      error?: string;
      warning?: string;
      // "write" = modifies data (red/danger), "read" = held read-only (blue/neutral)
      confirmKind?: "read" | "write";
      // Single-use token bound to the reviewed SQL. /execute runs the server-stored
      // statement when this is present, so a confirmed write can't be altered here.
      executionToken?: string;
      sourceQuery?: string;
      autoFixed?: boolean;
      confidence?: "high" | "medium" | "low";
      aiProvider?: string;
      // Whether the semantic labels used were AI-enhanced or rule-based.
      mode?: "ai" | "rule";
      explanation?: Explanation;
      meta?: {
        request_id?: string;
        latency_ms?: number;
        stage_timings?: Record<string, number>;
        compiler_version?: string;
        schema_hash?: string;
      };
    };

// ---------- No mock databases — users connect their own ----------
const initialDatabases: DB[] = [];

const suggestions = [
  "Show total revenue last month",
  "Top 10 customers by lifetime value",
  "Daily active users for the past 30 days",
  "Conversion rate by traffic source this quarter",
];

// ---------- App ----------
function DBBuddyApp() {
  const { hasPermission } = useAuth();
  const canPublish = hasPermission("chart:publish");
  const [theme, setTheme] = useState<"dark" | "light">("dark");
  const [section, setSection] = useState<Section>("chat");
  const [databases, setDatabases] = useState<DB[]>(initialDatabases);
  const [activeDb, setActiveDb] = useState<string>("");
  // Per-connection query history, keyed by connection id. Loaded from the
  // backend (account-scoped) and cached here.
  const [queryHistories, setQueryHistories] = useState<Record<string, HistoryItem[]>>({});
  // Connection ids whose history has been fetched at least once.
  const historyLoadedRef = useRef<Set<string>>(new Set());
  // Saved charts (Infographics), account-scoped, loaded from the backend.
  const [savedCharts, setSavedCharts] = useState<SavedChart[]>([]);
  const [connectOpen, setConnectOpen] = useState(false);
  // When set, the connect dialog opens in "edit" mode for this connection.
  const [editingDb, setEditingDb] = useState<DB | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [analysisLoading, setAnalysisLoading] = useState(false);
  const [analysisResult, setAnalysisResult] = useState<SemanticAnalysisResult | null>(null);
  const [analysisError, setAnalysisError] = useState<string | null>(null);
  // The active AI provider is an org-level record (Settings → AI Providers),
  // resolved server-side per request. This counter is bumped whenever the user
  // switches the active provider, so the schema is re-analyzed with the new model.
  const [providerRev, setProviderRev] = useState(0);
  // When off, read-only SELECT queries are held for confirmation. Persisted.
  const [autoExecuteReads, setAutoExecuteReads] = useState<boolean>(
    () => localStorage.getItem("auto_execute_reads") !== "false",
  );
  const scrollRef = useRef<HTMLDivElement>(null);
  // Skip the initial render so changing the provider re-analyzes, but mounting
  // (which already auto-analyzes via the activeDb effect) does not double-run.
  const providerMountedRef = useRef(false);

  // Surface a single consistent toast whenever the backend rejects an action
  // for lack of permission (RBAC 403), instead of a generic failure message.
  useEffect(() => {
    setForbiddenHandler((detail) =>
      toast.error("You don't have access to do that", {
        description: detail || "Your role doesn't permit this action.",
      }),
    );
    return () => setForbiddenHandler(null);
  }, []);

  // Active DB object
  const activeDbObj = useMemo(
    () => databases.find((d) => d.name === activeDb) ?? null,
    [databases, activeDb],
  );

  // The active connection's history (most recent first), from the backend cache.
  const activeHistory = useMemo<HistoryItem[]>(
    () => (activeDbObj ? (queryHistories[activeDbObj.id] ?? []) : []),
    [queryHistories, activeDbObj],
  );

  // Sidebar "recent queries" — the active DB's history as deduplicated text.
  const queryHistory = useMemo(
    () => [...new Map(activeHistory.map((h) => [h.query, h.query])).values()].slice(0, 10),
    [activeHistory],
  );

  // Append a completed query to the connection's history (persisted in backend).
  async function recordHistory(
    db: DB,
    query: string,
    status: "success" | "error",
    confidence?: "high" | "medium" | "low",
    sql?: string,
  ) {
    try {
      const created = await historyApi.add({
        nl_query: query,
        sql: sql ?? null,
        status,
        confidence: confidence ?? null,
        database_connection_id: db.id,
      });
      const item = apiHistoryToItem(created);
      setQueryHistories((prev) => ({ ...prev, [db.id]: [item, ...(prev[db.id] ?? [])] }));
    } catch (e) {
      console.error("Failed to record history:", e);
    }
  }

  // Clear the active connection's history. Saved (pinned) queries are kept.
  async function clearActiveHistory() {
    if (!activeDbObj) return;
    const connId = activeDbObj.id;
    try {
      await historyApi.clear(connId);
      setQueryHistories((prev) => ({
        ...prev,
        [connId]: (prev[connId] ?? []).filter((h) => h.pinned),
      }));
    } catch (e) {
      console.error("Failed to clear history:", e);
    }
  }

  // Delete a single history entry (the only way to remove a saved/pinned query).
  async function deleteHistoryItem(id: string) {
    if (!activeDbObj) return;
    const connId = activeDbObj.id;
    try {
      await historyApi.remove(id);
      setQueryHistories((prev) => ({
        ...prev,
        [connId]: (prev[connId] ?? []).filter((h) => h.id !== id),
      }));
    } catch (e) {
      console.error("Failed to delete history item:", e);
    }
  }

  // Whether the active DB has this query saved (pinned).
  function isQuerySaved(nlQuery?: string): boolean {
    if (!nlQuery || !activeDbObj) return false;
    return (queryHistories[activeDbObj.id] ?? []).some((h) => h.query === nlQuery && h.pinned);
  }

  // Toggle "saved" on a query from the Generated SQL panel. Pins the matching
  // history entry (creating one if needed) so it persists until deleted.
  async function toggleSaveQuery(nlQuery: string, sql: string) {
    if (!activeDbObj || !nlQuery) return;
    const connId = activeDbObj.id;
    const existing = (queryHistories[connId] ?? []).find((h) => h.query === nlQuery);
    try {
      if (existing) {
        const updated = apiHistoryToItem(await historyApi.setPinned(existing.id, !existing.pinned));
        setQueryHistories((prev) => ({
          ...prev,
          [connId]: (prev[connId] ?? []).map((h) => (h.id === updated.id ? updated : h)),
        }));
      } else {
        const created = apiHistoryToItem(
          await historyApi.add({
            nl_query: nlQuery,
            sql: sql ?? null,
            status: "success",
            pinned: true,
            database_connection_id: connId,
          }),
        );
        setQueryHistories((prev) => ({ ...prev, [connId]: [created, ...(prev[connId] ?? [])] }));
      }
    } catch (e) {
      console.error("Failed to toggle saved query:", e);
    }
  }

  // Save a chart to Infographics (config only — re-run live when viewed).
  async function saveChart(opts: {
    sql: string;
    title: string;
    nlQuery?: string;
    columns: string[];
    chartType?: SavedChart["chartType"];
  }) {
    if (!activeDbObj || !opts.sql.trim()) return;
    try {
      const created = await chartsApi.create({
        title: opts.title.trim() || opts.nlQuery || opts.sql,
        nl_query: opts.nlQuery ?? null,
        sql: opts.sql,
        chart_type: opts.chartType ?? "bar",
        schema_fingerprint: columnFingerprint(opts.columns),
        database_connection_id: activeDbObj.id,
      });
      setSavedCharts((prev) => [apiChartToSaved(created), ...prev]);
      toast.success("Chart saved to Infographics.");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed to save chart.");
    }
  }

  async function deleteChart(id: string) {
    try {
      await chartsApi.remove(id);
      setSavedCharts((prev) => prev.filter((c) => c.id !== id));
    } catch (e) {
      console.error("Failed to delete chart:", e);
    }
  }

  async function deleteAllCharts() {
    try {
      await chartsApi.removeAll();
      setSavedCharts([]);
    } catch (e) {
      console.error("Failed to delete all charts:", e);
    }
  }

  // Persist a chart's visual customization (type / colors) from Infographics.
  async function updateChart(
    id: string,
    patch: { chartType?: ChartType; config?: ChartConfig | null },
  ) {
    // Optimistic: reflect the change immediately, roll back on failure.
    const prevSnapshot = savedCharts.find((c) => c.id === id);
    setSavedCharts((prev) =>
      prev.map((c) =>
        c.id === id
          ? {
              ...c,
              chartType: patch.chartType ?? c.chartType,
              config: patch.config !== undefined ? patch.config : c.config,
            }
          : c,
      ),
    );
    try {
      await chartsApi.update(id, {
        chart_type: patch.chartType,
        config: patch.config as Record<string, unknown> | null | undefined,
      });
    } catch (e) {
      if (prevSnapshot) {
        setSavedCharts((prev) => prev.map((c) => (c.id === id ? prevSnapshot : c)));
      }
      toast.error(e instanceof Error ? e.message : "Failed to save chart changes.");
    }
  }

  async function publishChart(id: string) {
    try {
      await chartsApi.publish(id);
      setSavedCharts((prev) => prev.map((c) => (c.id === id ? { ...c, status: "published" } : c)));
      toast.success("Chart published — visible to your organization");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed to publish chart.");
    }
  }

  async function unpublishChart(id: string) {
    try {
      await chartsApi.unpublish(id);
      setSavedCharts((prev) => prev.map((c) => (c.id === id ? { ...c, status: "draft" } : c)));
      toast.success("Chart unpublished");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed to unpublish chart.");
    }
  }

  useEffect(() => {
    const root = document.documentElement;
    root.classList.toggle("light", theme === "light");
    root.classList.toggle("dark", theme === "dark");
  }, [theme]);

  useEffect(() => {
    localStorage.setItem("auto_execute_reads", String(autoExecuteReads));
  }, [autoExecuteReads]);

  // Connections live in the application database (tied to the account), not in
  // localStorage. One-time migration: if the backend has none but the browser
  // has saved connections, import them into the account, then drop the local
  // copies — so existing users keep their connections after upgrading.
  async function importLocalConnections(): Promise<boolean> {
    type LocalConn = {
      host: string;
      user: string;
      database: string;
      name?: string;
      engine?: string;
      password?: string;
    };
    let raw: LocalConn[] = [];
    try {
      const list = localStorage.getItem(DB_LIST_KEY);
      if (list) {
        raw = JSON.parse(list);
      } else {
        const legacy = localStorage.getItem(LEGACY_DB_CONFIG_KEY);
        if (legacy) raw = [JSON.parse(legacy)];
      }
    } catch {
      raw = [];
    }
    const valid = (Array.isArray(raw) ? raw : []).filter(
      (d) => d && d.host && d.user && d.database,
    );
    if (valid.length === 0) return false;

    for (const d of valid) {
      try {
        await connectionsApi.create({
          name: d.name || "Imported connection",
          engine: (d.engine || "mysql").toLowerCase(),
          host: d.host,
          username: d.user,
          password: d.password || "",
          database: d.database,
          port: null,
        });
      } catch (e) {
        console.error("Failed to import connection", d?.name, e);
      }
    }
    // Local copies (including plaintext passwords) are no longer needed.
    localStorage.removeItem(DB_LIST_KEY);
    localStorage.removeItem(LEGACY_DB_CONFIG_KEY);
    toast.success(`Imported ${valid.length} saved connection(s) to your account.`);
    return true;
  }

  // One-time migration of localStorage history + charts into the account,
  // mapping each old dbKey to its (now backend) connection id. Runs once: the
  // localStorage keys are removed afterward.
  async function importLocalHistoryAndCharts(conns: ApiConnection[]) {
    const idByKey: Record<string, string> = {};
    conns.forEach((c) => {
      idByKey[dbKey({ host: c.host, database: c.database, engine: c.engine })] = c.id;
    });

    const histRaw = localStorage.getItem(HISTORY_KEY);
    if (histRaw) {
      try {
        type LocalHistItem = {
          query: string;
          sql?: string | null;
          status?: string;
          confidence?: string | null;
          pinned?: boolean;
        };
        const map = JSON.parse(histRaw) as Record<string, LocalHistItem[]>;
        for (const [k, items] of Object.entries(map)) {
          const connId = idByKey[k] ?? null;
          for (const it of items ?? []) {
            await historyApi.add({
              nl_query: it.query,
              sql: it.sql ?? null,
              status: it.status ?? "success",
              confidence: it.confidence ?? null,
              pinned: !!it.pinned,
              database_connection_id: connId,
            });
          }
        }
      } catch (e) {
        console.error("Failed to import local history:", e);
      }
      localStorage.removeItem(HISTORY_KEY);
    }

    const chartRaw = localStorage.getItem(INFOGRAPHICS_KEY);
    if (chartRaw) {
      try {
        const arr = JSON.parse(chartRaw);
        for (const ch of Array.isArray(arr) ? arr : []) {
          await chartsApi.create({
            title: ch.title || ch.sql,
            nl_query: ch.nlQuery ?? null,
            sql: ch.sql,
            chart_type: "bar",
            schema_fingerprint: ch.schemaFingerprint ?? null,
            database_connection_id: idByKey[ch.databaseKey ?? ch.dbKey] ?? null,
          });
        }
      } catch (e) {
        console.error("Failed to import local charts:", e);
      }
      localStorage.removeItem(INFOGRAPHICS_KEY);
    }
  }

  // Load the account's connections + saved charts on mount (after auth).
  useEffect(() => {
    (async () => {
      try {
        let conns = await connectionsApi.list();
        if (conns.length === 0 && (await importLocalConnections())) {
          conns = await connectionsApi.list();
        }
        // One-time import of any local history/charts, then drop the local copies.
        await importLocalHistoryAndCharts(conns);
        const dbs = conns.map(apiConnToDb);
        setDatabases(dbs);
        if (dbs.length > 0) setActiveDb(dbs[0].name);
        // Surface any connection whose stored credentials can no longer be
        // decrypted up front — a visible maintenance task, not a surprise at
        // query time.
        const stale = dbs.filter((d) => !d.credentialsOk);
        if (stale.length > 0) {
          toast.warning(
            stale.length === 1
              ? `“${stale[0].name}”: saved credentials can’t be decrypted. Edit the connection and re-enter the password.`
              : `${stale.length} connections need their password re-entered (saved credentials can’t be decrypted).`,
            { duration: 8000 },
          );
        }
        setSavedCharts((await chartsApi.list()).map(apiChartToSaved));
      } catch (e) {
        console.error("Failed to load account data:", e);
      }
    })();
  }, []);

  // Load the active connection's history the first time it becomes active.
  useEffect(() => {
    if (!activeDbObj) return;
    const connId = activeDbObj.id;
    if (historyLoadedRef.current.has(connId)) return;
    historyLoadedRef.current.add(connId);
    (async () => {
      try {
        const items = (await historyApi.list(connId)).map(apiHistoryToItem);
        setQueryHistories((prev) => ({ ...prev, [connId]: items }));
      } catch (e) {
        console.error("Failed to load history:", e);
      }
    })();
  }, [activeDbObj]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  // Auto-analyze when active DB changes (only if it has credentials).
  // Queries are usable immediately (backend serves fast rule-based labels);
  // this runs in the background and upgrades to AI-enhanced labels when done.
  useEffect(() => {
    if (activeDbObj && activeDbObj.host && activeDbObj.user && activeDbObj.database) {
      analyzeDB(activeDbObj);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeDb]);

  // Re-analyze when the active AI provider changes — never silently mix models.
  // The persisted semantic layer is rebuilt with the newly selected provider.
  useEffect(() => {
    if (!providerMountedRef.current) {
      providerMountedRef.current = true;
      return; // initial mount already handled by the activeDb effect
    }
    if (activeDbObj && activeDbObj.host && activeDbObj.user && activeDbObj.database) {
      analyzeDB(activeDbObj);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [providerRev]);

  // AI readiness for the hybrid UX banner:
  //   analyzing → background Analyze in flight
  //   ai        → AI-enhanced labels active
  //   fast      → rule-based labels (instant; Analyze not done or AI unavailable)
  const aiStatus: "fast" | "analyzing" | "ai" = analysisLoading
    ? "analyzing"
    : analysisResult?.metadata?.ai_used
      ? "ai"
      : "fast";

  const lastAssistant = useMemo(
    () =>
      [...messages]
        .reverse()
        .find(
          (m): m is Extract<Message, { role: "assistant" }> =>
            m.role === "assistant" && m.status === "success",
        ),
    [messages],
  );

  async function ask(text: string) {
    if (!text.trim() || sending) return;

    const db = activeDbObj;
    if (!db) {
      setConnectOpen(true);
      return;
    }

    // Non-blocking guard: if AI analysis is in flight (e.g. just connected or
    // changed model), let the query run but inform the user it may use
    // rule-based labels until analysis finishes.
    if (analysisLoading) {
      toast.info("Re-analyzing schema — results may use rule-based labels until it finishes.");
    }

    const userMsg: Message = { id: crypto.randomUUID(), role: "user", text };
    const loadingId = crypto.randomUUID();
    const loadingMsg: Message = {
      id: loadingId,
      role: "assistant",
      text: "",
      sql: "",
      status: "loading",
      sourceQuery: text,
    };
    setMessages((m) => [...m, userMsg, loadingMsg]);
    setInput("");
    setSending(true);

    try {
      const startMs = Date.now();
      const res = await apiFetch("/query", {
        method: "POST",
        body: JSON.stringify({
          connection_id: db.id,
          question: text,
          ai: true,
          auto_execute_reads: autoExecuteReads,
        }),
      });

      const data = await readJson<QueryApiResponse>(res);

      if (!res.ok) {
        throw new Error(
          data?.detail || `Query failed (HTTP ${res.status}). Is the backend running?`,
        );
      }
      if (!data) {
        throw new Error("The backend returned an empty response.");
      }

      const elapsedMs = Date.now() - startMs;

      // CRITICAL: Check for error first before anything else
      if (data.error) {
        setMessages((m) =>
          m.map((msg) =>
            msg.id === loadingId
              ? {
                  ...msg,
                  status: "error" as const,
                  sql: data.sql ?? "",
                  text: "",
                  error: data.error,
                  confidence: data.confidence ?? "low",
                  aiProvider: db.engine,
                }
              : msg,
          ),
        );
        recordHistory(db, text, "error", data.confidence ?? "low");
        return;
      }

      // Generated but held for approval — show Run / Cancel. Only an actual
      // data-modifying query may claim it "will modify data"; a held read-only
      // query (e.g. needs clarification) must not.
      if (!data.auto_executed) {
        const writeTypes = ["insert", "update", "delete", "drop", "alter", "truncate", "create"];
        const isWrite =
          data.requires_confirmation === true ||
          writeTypes.includes(String(data.query_type ?? "").toLowerCase());
        const fallbackWarning = isWrite
          ? "This query will modify data. Review the SQL before executing."
          : "Review the generated SQL before running.";
        setMessages((m) =>
          m.map((msg) =>
            msg.id === loadingId
              ? {
                  ...msg,
                  status: "pending_confirmation" as const,
                  sql: data.sql ?? "",
                  text: "",
                  warning: data.warning ?? fallbackWarning,
                  confirmKind: isWrite ? "write" : "read",
                  executionToken: data.execution_token,
                  confidence: data.confidence ?? "medium",
                  aiProvider: db.engine,
                  sourceQuery: text,
                  result: {
                    columns: [],
                    rows: [],
                    timeMs: 0,
                    source: db.name,
                    semantic: [],
                    termInterpretations: data.term_interpretations,
                    relevanceReason: data.relevance_check?.reason,
                  },
                }
              : msg,
          ),
        );
        setSending(false);
        return;
      }

      // SELECT with results
      const rawRows: Record<string, unknown>[] = data.results ?? [];
      const columns = rawRows.length > 0 ? Object.keys(rawRows[0]) : [];
      const rows = rawRows.map((r) => columns.map((c) => r[c] as string | number));

      // Build semantic interpretation from semantic_layer
      const semanticLayer: Record<
        string,
        Record<string, { term: string; source?: string }>
      > = data.semantic_layer ?? {};
      const semantic: { from: string; to: string }[] = columns
        .map((col) => {
          for (const table of Object.values(semanticLayer)) {
            if (table[col]) return { from: table[col].term, to: col };
          }
          return null;
        })
        .filter(Boolean) as { from: string; to: string }[];

      // Were the labels AI-enhanced or rule-based? Drives the analyst-facing
      // execution badge ("AI-enhanced" vs "Rule-based · Deterministic").
      // `semantic_layer` here is only the slice of columns this query touched,
      // so the backend sends `ai_labeled` computed over the whole layer — the
      // badge must not flip just because one query hit rule-based columns.
      const aiLabels =
        data.ai_labeled ??
        Object.values(semanticLayer).some((table) =>
          Object.values(table).some((c) => c?.source === "ai"),
        );
      // Honest query latency from the backend (not wall-clock, which includes
      // network and any concurrent background analyze).
      const queryMs = Math.round(data.meta?.latency_ms ?? elapsedMs);

      setMessages((m) =>
        m.map((msg) =>
          msg.id === loadingId
            ? {
                ...msg,
                status: "success" as const,
                sql: data.sql ?? "",
                text: data.auto_fixed
                  ? "Query was automatically repaired and re-executed."
                  : `Here are the results for: "${text}"`,
                result: {
                  columns,
                  rows,
                  timeMs: queryMs,
                  source: db.name,
                  semantic,
                  termInterpretations: data.term_interpretations,
                  relevanceReason: data.relevance_check?.reason,
                },
                confidence: data.confidence ?? "high",
                autoFixed: data.auto_fixed ?? false,
                aiProvider: db.engine,
                mode: aiLabels ? "ai" : "rule",
                explanation: data.explanation,
                meta: data.meta,
              }
            : msg,
        ),
      );
      recordHistory(db, text, "success", data.confidence ?? "high", data.sql);
    } catch (err) {
      setMessages((m) =>
        m.map((msg) =>
          msg.id === loadingId
            ? {
                ...msg,
                status: "error" as const,
                sql: "",
                text: "",
                error: err instanceof Error ? err.message : "Something went wrong.",
              }
            : msg,
        ),
      );
      recordHistory(db, text, "error");
    } finally {
      setSending(false);
    }
  }

  async function analyzeDB(dbOverride?: DB) {
    const db = dbOverride ?? databases.find((item) => item.name === activeDb);

    if (!db || !db.host || !db.user || !db.database) {
      setAnalysisError(
        "Select a connected database and provide host, user, and database details first.",
      );
      return;
    }

    setAnalysisLoading(true);
    setAnalysisError(null);

    try {
      const res = await apiFetch("/analyze", {
        method: "POST",
        body: JSON.stringify({
          connection_id: db.id,
          ai: true,
        }),
      });

      const data = await readJson<SemanticAnalysisResult & { detail?: string }>(res);
      if (!res.ok) {
        throw new Error(
          data?.detail || `Analysis request failed (HTTP ${res.status}). Is the backend running?`,
        );
      }
      if (!data) {
        throw new Error("The backend returned an empty response.");
      }

      setAnalysisResult(data as SemanticAnalysisResult);
      setSection("chat");
    } catch (error) {
      setAnalysisError(
        error instanceof Error ? error.message : "Unable to analyze the database schema.",
      );
    } finally {
      setAnalysisLoading(false);
    }
  }

  function newChat() {
    setMessages([]);
    setInput("");
    setSection("chat");
  }

  // Disconnect a single database (the one named), leaving all others intact.
  // Removes the connection from the account; a reload won't bring it back.
  function disconnectDb(name: string) {
    const removed = databases.find((d) => d.name === name);
    const remaining = databases.filter((d) => d.name !== name);
    setDatabases(remaining);

    // If we removed the active DB, switch to a remaining one (or none).
    if (activeDb === name) {
      const next = remaining[0]?.name ?? "";
      setActiveDb(next);
      setMessages([]);
      setAnalysisResult(null);
    }

    if (removed) {
      // Delete the connection from the backend (best-effort).
      connectionsApi.remove(removed.id).catch((e) => console.error("Disconnect failed:", e));
      // Disconnecting clears that database's local query history too.
      const key = dbKey(removed);
      setQueryHistories((prev) => {
        if (!(key in prev)) return prev;
        const next = { ...prev };
        delete next[key];
        return next;
      });
    }
  }

  // Remove every connection from the account (Settings → Disconnect all).
  async function clearAllConnections() {
    try {
      await connectionsApi.removeAll();
    } catch (e) {
      console.error("Failed to clear connections:", e);
    }
    setDatabases([]);
    setActiveDb("");
    setMessages([]);
    setAnalysisResult(null);
    setQueryHistories({});
    historyLoadedRef.current.clear();
    toast.success("All database connections removed.");
  }

  function regenerate(sourceQuery?: string) {
    if (sourceQuery) ask(sourceQuery);
  }

  async function executeSQL(messageId: string, sql: string, executionToken?: string) {
    const db = activeDbObj;
    if (!db || !sql) return;

    // Mark message as loading while executing
    setMessages((m) =>
      m.map((msg) => (msg.id === messageId ? { ...msg, status: "loading" as const } : msg)),
    );
    setSending(true);

    try {
      const startMs = Date.now();
      // A confirmed write carries an execution_token — the server runs the exact
      // SQL it stored for that plan, so this `sql` (display only) can't override it.
      const res = await apiFetch("/execute", {
        method: "POST",
        body: JSON.stringify({
          connection_id: db.id,
          sql,
          ...(executionToken ? { execution_token: executionToken } : {}),
        }),
      });
      const data = await readJson<ExecuteApiResponse>(res);
      const elapsedMs = Date.now() - startMs;

      if (!res.ok || !data || data.error) {
        setMessages((m) =>
          m.map((msg) =>
            msg.id === messageId
              ? {
                  ...msg,
                  status: "error" as const,
                  error:
                    data?.detail ??
                    data?.error ??
                    (res.ok
                      ? "The backend returned an empty response."
                      : `Execution failed (HTTP ${res.status}). Is the backend running?`),
                }
              : msg,
          ),
        );
        return;
      }

      // Write query (DELETE/UPDATE/INSERT) — no result rows, just rows_affected
      if (data.write) {
        setMessages((m) =>
          m.map((msg) =>
            msg.id === messageId
              ? {
                  ...msg,
                  status: "success" as const,
                  text: data.message ?? "Query executed successfully.",
                  result: {
                    columns: ["rows_affected"],
                    rows: [[data.rows_affected ?? 0]],
                    timeMs: elapsedMs,
                    source: db.name,
                    semantic: [],
                    termInterpretations:
                      msg.role === "assistant" ? msg.result?.termInterpretations : undefined,
                    relevanceReason:
                      msg.role === "assistant" ? msg.result?.relevanceReason : undefined,
                  },
                  confidence: "high" as const,
                }
              : msg,
          ),
        );
        return;
      }

      // SELECT — normal result set
      const rawRows: Record<string, unknown>[] = data.results ?? [];
      const columns = rawRows.length > 0 ? Object.keys(rawRows[0]) : [];
      const rows = rawRows.map((r) => columns.map((c) => r[c] as string | number));

      setMessages((m) =>
        m.map((msg) =>
          msg.id === messageId
            ? {
                ...msg,
                status: "success" as const,
                text: "Executed successfully.",
                result: {
                  columns,
                  rows,
                  timeMs: elapsedMs,
                  source: db.name,
                  semantic: [],
                  termInterpretations:
                    msg.role === "assistant" ? msg.result?.termInterpretations : undefined,
                  relevanceReason:
                    msg.role === "assistant" ? msg.result?.relevanceReason : undefined,
                },
                confidence: "high" as const,
              }
            : msg,
        ),
      );
    } catch (err) {
      setMessages((m) =>
        m.map((msg) =>
          msg.id === messageId
            ? {
                ...msg,
                status: "error" as const,
                error: err instanceof Error ? err.message : "Execution failed.",
              }
            : msg,
        ),
      );
    } finally {
      setSending(false);
    }
  }

  function dismissMessage(messageId: string) {
    setMessages((m) => m.filter((msg) => msg.id !== messageId));
  }

  return (
    <div className="flex h-screen w-full overflow-hidden bg-background text-foreground">
      <Toaster position="bottom-right" />
      <Sidebar
        section={section}
        setSection={setSection}
        databases={databases}
        activeDb={activeDb}
        setActiveDb={(name) => {
          setActiveDb(name);
          setMessages([]);
          setAnalysisResult(null);
        }}
        onConnect={() => setConnectOpen(true)}
        onEditDb={(db) => {
          setEditingDb(db);
          setConnectOpen(true);
        }}
        theme={theme}
        toggleTheme={() => setTheme(theme === "dark" ? "light" : "dark")}
        onNewChat={newChat}
        onRerun={ask}
        queryHistory={queryHistory}
        messages={messages}
      />

      <main className="relative flex min-w-0 flex-1 flex-col">
        <div
          className="pointer-events-none absolute inset-x-0 top-0 h-64 opacity-60"
          style={{ background: "var(--gradient-glow)" }}
        />
        <TopBar
          activeDb={activeDb}
          databases={databases}
          setActiveDb={(name) => {
            setActiveDb(name);
            setMessages([]);
            setAnalysisResult(null);
          }}
          onNewChat={newChat}
          onAnalyze={() => analyzeDB()}
          analysisLoading={analysisLoading}
          aiStatus={aiStatus}
          hasDb={!!activeDbObj}
          onConnect={() => setConnectOpen(true)}
          onDisconnect={disconnectDb}
        />

        <div className="relative flex min-h-0 flex-1">
          {section === "history" ? (
            <QueryHistoryPanel
              history={activeHistory}
              hasDb={!!activeDbObj}
              onRerun={ask}
              onClear={clearActiveHistory}
              onDelete={deleteHistoryItem}
              onLoadQuery={(query) => {
                setInput(query);
                setSection("chat");
              }}
            />
          ) : section === "infographics" ? (
            <InfographicsPanel
              charts={savedCharts}
              databases={databases}
              onDelete={deleteChart}
              onDeleteAll={deleteAllCharts}
              onPublish={publishChart}
              onUnpublish={unpublishChart}
              onUpdateChart={updateChart}
              canPublish={canPublish}
              canViewRelations={hasPermission("schema:analyze")}
            />
          ) : section === "schedules" ? (
            <SchedulesPanel />
          ) : section === "settings" ? (
            <SettingsPanel
              theme={theme}
              toggleTheme={() => setTheme(theme === "dark" ? "light" : "dark")}
              onProviderActivated={() => setProviderRev((v) => v + 1)}
              autoExecuteReads={autoExecuteReads}
              setAutoExecuteReads={setAutoExecuteReads}
              onBack={() => setSection("chat")}
              onClearAllConnections={clearAllConnections}
            />
          ) : (
            <div className="flex min-w-0 flex-1 flex-col">
              <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 sm:px-8 py-6">
                {messages.length === 0 ? (
                  <EmptyState
                    onPick={ask}
                    hasDb={!!activeDbObj}
                    onConnect={() => setConnectOpen(true)}
                  />
                ) : (
                  <div className="mx-auto flex max-w-4xl flex-col gap-6 pb-4">
                    {messages.map((m) => (
                      <div key={m.id} className="animate-fade-up">
                        {m.role === "user" ? (
                          <UserBubble text={m.text} />
                        ) : (
                          <AssistantBubble
                            message={m}
                            onRegenerate={() => regenerate(m.sourceQuery)}
                            onEdit={() => m.sourceQuery && setInput(m.sourceQuery)}
                            onConfirm={(sql) => executeSQL(m.id, sql, m.executionToken)}
                            onCancel={() => dismissMessage(m.id)}
                            onSaveChart={saveChart}
                            canSaveChart={!!activeDbObj}
                            connectionId={activeDbObj?.id ?? null}
                            canViewInsights={hasPermission("schema:analyze")}
                            onToggleSaveQuery={toggleSaveQuery}
                            isQuerySaved={isQuerySaved(m.sourceQuery)}
                          />
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>

              <Composer
                value={input}
                setValue={setInput}
                onSend={() => ask(input)}
                sending={sending}
                hasDb={!!activeDbObj}
              />
            </div>
          )}

          {/* Right panel - only show in chat section */}
          {section === "chat" && (
            <RightPanel
              result={lastAssistant?.result}
              resultMessageId={lastAssistant?.id}
              analysisResult={analysisResult}
              analysisError={analysisError}
              hasConnectedDb={databases.some((db) => db.status === "connected")}
              onConnect={() => setConnectOpen(true)}
              activeDb={activeDbObj}
              analysisLoading={analysisLoading}
            />
          )}
        </div>
      </main>

      <ConnectDatabaseModal
        open={connectOpen}
        editing={editingDb}
        onOpenChange={(open) => {
          setConnectOpen(open);
          if (!open) setEditingDb(null);
        }}
        onConnect={async (fields) => {
          // Persist the connection in the account (credentials encrypted at rest);
          // the returned record (no password) becomes the active database.
          const created = await connectionsApi.create({
            name: fields.name,
            engine: fields.engine.toLowerCase(),
            host: fields.host,
            username: fields.user,
            password: fields.password,
            database: fields.database,
            port: fields.port,
          });
          const db = apiConnToDb(created);
          setDatabases((d) => [...d, db]);
          setActiveDb(db.name);
          setMessages([]);
          setAnalysisResult(null);
        }}
        onUpdate={async (id, fields) => {
          // Edit an existing connection. An empty password is omitted so the
          // stored one is kept; a new password re-encrypts (and heals a stale key).
          const updated = await connectionsApi.update(id, {
            name: fields.name,
            engine: fields.engine.toLowerCase(),
            host: fields.host,
            username: fields.user,
            database: fields.database,
            port: fields.port,
            ...(fields.password ? { password: fields.password } : {}),
          });
          const db = apiConnToDb(updated);
          setDatabases((list) => list.map((d) => (d.id === id ? db : d)));
        }}
      />
    </div>
  );
}

// ---------- Sidebar ----------
function Sidebar(props: {
  section: Section;
  setSection: (s: Section) => void;
  databases: DB[];
  activeDb: string;
  setActiveDb: (s: string) => void;
  onConnect: () => void;
  onEditDb: (db: DB) => void;
  theme: "dark" | "light";
  toggleTheme: () => void;
  onNewChat: () => void;
  onRerun: (q: string) => void;
  queryHistory: string[];
  messages: Message[];
}) {
  const nav = [
    { id: "chat", label: "Chat", icon: MessageSquare },
    { id: "history", label: "Query History", icon: History },
    { id: "infographics", label: "Infographics", icon: LineChart },
    { id: "schedules", label: "Schedules", icon: Clock },
    { id: "settings", label: "Settings", icon: Settings },
  ] as const;

  return (
    <aside className="hidden w-72 shrink-0 flex-col border-r border-sidebar-border bg-sidebar text-sidebar-foreground md:flex">
      <div className="group flex items-center gap-3 px-5 py-5">
        <div className="flex h-9 w-9 items-center justify-center rounded-xl brand-gradient glow transition-transform duration-300 group-hover:scale-105">
          <Database className="h-5 w-5 text-primary-foreground" />
        </div>
        <div>
          <div className="font-display text-lg font-semibold leading-none tracking-tight">
            DB Buddy
          </div>
          <div className="mt-1 text-[11px] text-muted-foreground">Deterministic query engine</div>
        </div>
      </div>

      <div className="px-3">
        <Button
          className="w-full justify-start gap-2 brand-gradient text-primary-foreground hover:opacity-90"
          onClick={props.onNewChat}
        >
          <Plus className="h-4 w-4" />
          New chat
        </Button>
      </div>

      <nav className="mt-5 flex flex-col gap-1 px-3">
        {nav.map((n) => {
          const Icon = n.icon;
          const active = props.section === n.id;
          return (
            <button
              key={n.id}
              onClick={() => props.setSection(n.id)}
              className={cn(
                "group flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-all duration-200",
                active
                  ? "bg-sidebar-accent text-sidebar-accent-foreground"
                  : "text-muted-foreground hover:translate-x-0.5 hover:bg-sidebar-accent/60 hover:text-sidebar-foreground",
              )}
            >
              <Icon className="h-4 w-4 transition-transform duration-200 group-hover:scale-110" />
              {n.label}
            </button>
          );
        })}
      </nav>

      <div className="mt-4 px-3">
        <div className="flex items-center justify-between px-2">
          <div className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
            Recent queries
          </div>
          <History className="h-3 w-3 text-muted-foreground" />
        </div>
        <div className="mt-2 flex flex-col gap-0.5">
          {props.queryHistory.length === 0 ? (
            <p className="px-2 py-1 text-[11px] text-muted-foreground/60 italic">No queries yet</p>
          ) : (
            props.queryHistory.map((h) => {
              // Find the corresponding assistant message to get status and confidence
              const assistantMsg = props.messages.find(
                (m): m is Extract<Message, { role: "assistant" }> =>
                  m.role === "assistant" && m.sourceQuery === h,
              );
              const status = assistantMsg?.status;
              const confidence = assistantMsg?.confidence;

              return (
                <button
                  key={h}
                  onClick={() => props.onRerun(h)}
                  title={`Re-run: ${h}`}
                  className="group flex items-center gap-2 truncate rounded-md px-2 py-1.5 text-left text-xs text-muted-foreground transition-all duration-200 hover:translate-x-0.5 hover:bg-sidebar-accent/60 hover:text-sidebar-foreground"
                >
                  <Clock className="h-3 w-3 shrink-0 opacity-60 group-hover:text-primary" />
                  <span className="truncate flex-1">{h}</span>
                  {status === "success" && (
                    <Check className="h-3 w-3 shrink-0 text-[oklch(0.72_0.17_155)]" />
                  )}
                  {status === "error" && (
                    <AlertCircle className="h-3 w-3 shrink-0 text-destructive" />
                  )}
                  {confidence === "high" && (
                    <span className="text-[9px] text-[oklch(0.72_0.17_155)]"></span>
                  )}
                  {confidence === "medium" && <span className="text-[9px] text-amber-400">~</span>}
                  {confidence === "low" && <span className="text-[9px] text-destructive">!</span>}
                </button>
              );
            })
          )}
        </div>
      </div>

      <div className="mt-auto border-t border-sidebar-border px-3 py-4">
        <div className="mb-2 flex items-center justify-between px-2">
          <div className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
            Databases
          </div>
          <button
            onClick={props.onConnect}
            className="rounded p-1 text-muted-foreground hover:bg-sidebar-accent hover:text-foreground"
            aria-label="Connect database"
          >
            <Plus className="h-3.5 w-3.5" />
          </button>
        </div>
        <div className="flex flex-col gap-0.5">
          {props.databases.map((db) => (
            <div
              key={db.id}
              className={cn(
                "group flex items-center justify-between gap-1 rounded-md pr-1 text-sm transition-all duration-200",
                props.activeDb === db.name
                  ? "bg-sidebar-accent text-sidebar-accent-foreground"
                  : "hover:bg-sidebar-accent/60",
              )}
            >
              <button
                onClick={() => db.status === "connected" && props.setActiveDb(db.name)}
                className="flex min-w-0 flex-1 items-center gap-2 px-2 py-1.5 text-left"
              >
                <CircleDot
                  className={cn(
                    "h-3 w-3 shrink-0",
                    !db.credentialsOk
                      ? "text-amber-500"
                      : db.status === "connected"
                        ? "text-[oklch(0.72_0.17_155)]"
                        : "text-muted-foreground/50",
                  )}
                />
                <div className="min-w-0">
                  <div className="truncate text-xs font-medium">{db.name}</div>
                  <div className="truncate text-[10px] text-muted-foreground">
                    {!db.credentialsOk ? "Credentials need re-entry" : db.engine}
                  </div>
                </div>
              </button>
              {!db.credentialsOk && (
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    props.onEditDb(db);
                  }}
                  title="Saved credentials can’t be decrypted — click to re-enter the password"
                  aria-label={`Fix credentials for ${db.name}`}
                  className="shrink-0 rounded p-1 text-amber-500 hover:bg-sidebar-accent"
                >
                  <AlertTriangle className="h-3.5 w-3.5" />
                </button>
              )}
            </div>
          ))}
        </div>

        <button
          onClick={props.toggleTheme}
          className="mt-4 flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-xs text-muted-foreground hover:bg-sidebar-accent/60 hover:text-foreground"
        >
          {props.theme === "dark" ? (
            <Sun className="h-3.5 w-3.5" />
          ) : (
            <Moon className="h-3.5 w-3.5" />
          )}
          {props.theme === "dark" ? "Light theme" : "Dark theme"}
        </button>

        <AccountFooter />
      </div>
    </aside>
  );
}

// Account block at the bottom of the sidebar: who's signed in + logout.
function AccountFooter() {
  const { user, logout } = useAuth();
  if (!user) return null;
  return (
    <div className="mt-2 border-t border-sidebar-border/60 pt-2">
      <div className="flex items-center gap-2 px-2 py-1.5">
        <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary/15 text-[11px] font-semibold uppercase text-primary">
          {(user.full_name || user.email).slice(0, 1)}
        </div>
        <div className="min-w-0 flex-1">
          <div className="truncate text-xs font-medium">{user.full_name || user.email}</div>
          <div className="truncate text-[10px] capitalize text-muted-foreground">
            {user.roles.join(", ") || "user"}
          </div>
        </div>
        <NotificationsBell />
      </div>
      <button
        onClick={() => logout()}
        className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-xs text-muted-foreground hover:bg-sidebar-accent/60 hover:text-foreground"
      >
        <LogOut className="h-3.5 w-3.5" /> Sign out
      </button>
    </div>
  );
}

// ---------- Top bar ----------
function AiModeBadge({ status }: { status: "fast" | "analyzing" | "ai" }) {
  if (status === "analyzing") {
    return (
      <span
        className="inline-flex items-center gap-1.5 rounded-full border border-accent/40 bg-accent/10 px-2.5 py-1 text-xs font-medium text-accent"
        title="Building AI-enhanced understanding in the background (~5s). You can query right now — first queries may be slower until indexing finishes."
      >
        <Loader2 className="h-3 w-3 animate-spin" />
        Indexing… first queries may be slower
      </span>
    );
  }
  if (status === "ai") {
    return (
      <span
        className="inline-flex items-center gap-1.5 rounded-full border border-emerald-500/40 bg-emerald-500/10 px-2.5 py-1 text-xs font-medium text-emerald-300"
        title="AI-enhanced semantic layer is active for this schema."
      >
        <Sparkles className="h-3 w-3" />
        AI-enhanced mode active
      </span>
    );
  }
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full border border-amber-500/40 bg-amber-500/10 px-2.5 py-1 text-xs font-medium text-amber-300"
      title="Fast rule-based labels. Click Analyze Schema for AI-enhanced understanding (~5s)."
    >
      <Zap className="h-3 w-3" />
      Fast mode (rule-based)
    </span>
  );
}

function TopBar({
  activeDb,
  databases,
  setActiveDb,
  onNewChat,
  onAnalyze,
  analysisLoading,
  aiStatus,
  hasDb,
  onConnect,
  onDisconnect,
}: {
  activeDb: string;
  databases: DB[];
  setActiveDb: (s: string) => void;
  onNewChat: () => void;
  onAnalyze: () => void;
  analysisLoading: boolean;
  aiStatus: "fast" | "analyzing" | "ai";
  hasDb: boolean;
  onConnect: () => void;
  onDisconnect: (name: string) => void;
}) {
  const [dbMenuOpen, setDbMenuOpen] = useState(false);
  // Second-level "Change Database" view inside the dropdown.
  const [showDbList, setShowDbList] = useState(false);

  function closeMenu() {
    setDbMenuOpen(false);
    setShowDbList(false);
  }

  return (
    <header className="relative z-10 flex items-center justify-between gap-4 border-b border-border/60 bg-background/80 px-4 sm:px-8 py-3 backdrop-blur">
      <div className="flex items-center gap-4">
        <div>
          <h1 className="font-display text-xl font-semibold tracking-tight sm:text-2xl">
            {activeDb ? (
              <>
                Connected to <span className="text-brand-gradient">{activeDb}</span>
              </>
            ) : (
              <>
                Connect a <span className="text-brand-gradient">database</span> to start
              </>
            )}
          </h1>
          <p className="text-xs text-muted-foreground">
            <Sparkles className="mr-1 inline h-3 w-3 text-accent" />
            Deterministic query planning + Schema-aware validation
          </p>
        </div>

        {/* Database dropdown menu */}
        {activeDb && (
          <div className="relative">
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                setDbMenuOpen(!dbMenuOpen);
                setShowDbList(false);
              }}
              className="gap-2"
            >
              <Database className="h-3.5 w-3.5" />
              {activeDb}
              <ChevronDown className="h-3.5 w-3.5" />
            </Button>

            {dbMenuOpen && (
              <div className="absolute top-full left-0 mt-2 w-56 rounded-lg border border-border bg-card shadow-lg z-50">
                {!showDbList ? (
                  <div className="p-1">
                    <Button
                      variant="ghost"
                      size="sm"
                      className="w-full justify-start gap-2 h-8 text-xs"
                      onClick={() => {
                        closeMenu();
                        // Reconnect logic - just refresh the connection
                        onAnalyze();
                      }}
                    >
                      <RefreshCw className="h-3 w-3" />
                      Reconnect
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="w-full justify-start gap-2 h-8 text-xs"
                      onClick={() => setShowDbList(true)}
                    >
                      <Database className="h-3 w-3" />
                      Change Database
                      <ChevronRight className="ml-auto h-3 w-3" />
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="w-full justify-start gap-2 h-8 text-xs text-destructive hover:text-destructive"
                      onClick={() => {
                        closeMenu();
                        onDisconnect(activeDb);
                      }}
                    >
                      <X className="h-3 w-3" />
                      Disconnect
                    </Button>
                  </div>
                ) : (
                  <div className="p-1">
                    <div className="flex items-center gap-1 px-1 pb-1">
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-6 w-6"
                        onClick={() => setShowDbList(false)}
                        aria-label="Back"
                      >
                        <ChevronLeft className="h-3.5 w-3.5" />
                      </Button>
                      <span className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                        Switch database
                      </span>
                    </div>
                    <div className="max-h-56 overflow-y-auto">
                      {databases.map((db) => (
                        <Button
                          key={db.id}
                          variant="ghost"
                          size="sm"
                          className="w-full justify-start gap-2 h-9 text-xs"
                          onClick={() => {
                            closeMenu();
                            if (db.name !== activeDb) setActiveDb(db.name);
                          }}
                        >
                          <Database className="h-3 w-3 shrink-0" />
                          <span className="flex flex-col items-start leading-tight">
                            <span className="truncate">{db.name}</span>
                            <span className="text-[10px] text-muted-foreground">{db.engine}</span>
                          </span>
                          {db.name === activeDb && (
                            <Check className="ml-auto h-3.5 w-3.5 text-primary" />
                          )}
                        </Button>
                      ))}
                    </div>
                    <div className="my-1 border-t border-border" />
                    <Button
                      variant="ghost"
                      size="sm"
                      className="w-full justify-start gap-2 h-8 text-xs text-primary"
                      onClick={() => {
                        closeMenu();
                        onConnect();
                      }}
                    >
                      <Plus className="h-3 w-3" />
                      Connect new database
                    </Button>
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {activeDb && <AiModeBadge status={aiStatus} />}
      </div>

      <div className="flex items-center gap-2">
        <Button variant="outline" size="sm" className="gap-1.5" onClick={onNewChat}>
          <Plus className="h-3.5 w-3.5" />
          New chat
        </Button>
        <Button
          size="sm"
          className="gap-1.5 brand-gradient text-primary-foreground"
          onClick={onAnalyze}
          disabled={!!(analysisLoading || !hasDb)}
          title={!hasDb ? "Connect a database first" : ""}
        >
          {analysisLoading ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <Sparkles className="h-3.5 w-3.5" />
          )}
          {analysisLoading ? "Analyzing…" : "Analyze Schema"}
        </Button>
      </div>
    </header>
  );
}

// ---------- Empty state ----------
function EmptyState({
  onPick,
  hasDb,
  onConnect,
}: {
  onPick: (s: string) => void;
  hasDb: boolean;
  onConnect: () => void;
}) {
  if (!hasDb) {
    return (
      <div className="mx-auto flex h-full max-w-md flex-col items-center justify-center gap-6 py-10 text-center animate-scale-in">
        <div className="flex h-16 w-16 items-center justify-center rounded-2xl brand-gradient glow animate-float">
          <Database className="h-7 w-7 text-primary-foreground" />
        </div>
        <div className="animate-fade-up delay-100">
          <h2 className="font-display text-2xl font-semibold tracking-tight">Connect a database</h2>
          <p className="mt-2 text-sm text-muted-foreground">
            Add your database credentials to start asking questions in plain English.
          </p>
        </div>
        <Button
          className="gap-2 brand-gradient text-primary-foreground animate-fade-up delay-200 interactive"
          onClick={onConnect}
        >
          <Plus className="h-4 w-4" /> Connect database
        </Button>
      </div>
    );
  }

  return (
    <div className="mx-auto flex h-full max-w-3xl flex-col items-center justify-center gap-8 py-10 text-center">
      <div className="flex h-16 w-16 items-center justify-center rounded-2xl brand-gradient glow animate-scale-in animate-float">
        <Sparkles className="h-7 w-7 text-primary-foreground" />
      </div>
      <div className="animate-fade-up delay-100">
        <h2 className="font-display text-3xl font-semibold tracking-tight">
          What do you want to know?
        </h2>
        <p className="mt-2 text-sm text-muted-foreground">
          DB Buddy uses deterministic planning to turn plain English into explainable SQL, validates
          it against your schema, and executes with confidence scoring.
        </p>
      </div>
      <div className="grid w-full grid-cols-1 gap-2 sm:grid-cols-2">
        {suggestions.map((s, i) => (
          <button
            key={s}
            onClick={() => onPick(s)}
            style={{ animationDelay: `${200 + i * 80}ms` }}
            className="group rounded-xl border border-border bg-card/60 p-4 text-left animate-fade-up transition-all duration-300 hover:-translate-y-0.5 hover:border-primary/60 hover:bg-card hover:shadow-[var(--shadow-soft)]"
          >
            <div className="flex items-start gap-3">
              <div className="rounded-md bg-primary/10 p-1.5 text-primary transition-all duration-200 group-hover:scale-110 group-hover:bg-primary/20">
                <Sparkles className="h-3.5 w-3.5" />
              </div>
              <span className="text-sm">{s}</span>
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}

// ---------- Bubbles ----------
function UserBubble({ text }: { text: string }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[80%] rounded-2xl rounded-tr-sm bg-primary px-4 py-2.5 text-sm text-primary-foreground shadow-[var(--shadow-soft)]">
        {text}
      </div>
    </div>
  );
}

function AssistantBubble({
  message,
  onRegenerate,
  onEdit,
  onConfirm,
  onCancel,
  onSaveChart,
  canSaveChart,
  connectionId,
  canViewInsights,
  onToggleSaveQuery,
  isQuerySaved,
}: {
  message: Extract<Message, { role: "assistant" }>;
  onRegenerate: () => void;
  onEdit: () => void;
  onConfirm: (sql: string) => void;
  onCancel: () => void;
  onSaveChart: (opts: {
    sql: string;
    title: string;
    nlQuery?: string;
    columns: string[];
    chartType?: SavedChart["chartType"];
  }) => void;
  canSaveChart: boolean;
  connectionId: string | null;
  // The Insights tab is analyst-only. The API enforces this (schema:analyze);
  // hiding the tab just avoids offering a control that would 403.
  canViewInsights: boolean;
  onToggleSaveQuery: (nlQuery: string, sql: string) => void;
  isQuerySaved: boolean;
}) {
  if (message.status === "loading") {
    return (
      <div className="flex items-start gap-3">
        <AssistantAvatar />
        <div className="flex items-center gap-3 rounded-2xl rounded-tl-sm border border-border bg-card px-4 py-3 text-sm text-muted-foreground shadow-[var(--shadow-soft)]">
          <div className="flex gap-1">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-primary [animation-delay:-0.3s]" />
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-primary [animation-delay:-0.15s]" />
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-primary" />
          </div>
          <span>AI is thinking — generating SQL…</span>
        </div>
      </div>
    );
  }

  if (message.status === "pending_confirmation") {
    // Read holds are informational (blue/neutral); writes are a danger
    // confirmation (red) so the user can tell at a glance whether it's risky.
    const isWrite = message.confirmKind !== "read";
    const tone = isWrite
      ? {
          card: "border-red-500/40 bg-red-500/10",
          badge: "bg-red-500/20 text-red-400",
          text: "text-red-200/80",
          label: "Confirmation required",
          runClass: "bg-destructive hover:bg-destructive/90 text-destructive-foreground",
        }
      : {
          card: "border-blue-500/40 bg-blue-500/10",
          badge: "bg-blue-500/20 text-blue-400",
          text: "text-blue-200/80",
          label: "Review before running",
          runClass: "brand-gradient text-primary-foreground hover:opacity-90",
        };
    return (
      <div className="flex items-start gap-3">
        <AssistantAvatar />
        <div className="min-w-0 flex-1 space-y-3">
          <div className={cn("rounded-2xl rounded-tl-sm border px-4 py-3 text-sm", tone.card)}>
            <div className="mb-2 flex items-center gap-2">
              <Badge className={cn("border-0 gap-1", tone.badge)}>
                <AlertCircle className="h-3 w-3" /> {tone.label}
              </Badge>
            </div>
            <p className={cn("text-sm mb-3", tone.text)}>
              {message.warning ?? "Review the generated SQL below before running."}
            </p>
            <div className="flex flex-wrap gap-2">
              <Button
                size="sm"
                className={cn("h-7 gap-1.5 text-xs", tone.runClass)}
                onClick={() => onConfirm(message.sql)}
              >
                <Check className="h-3 w-3" /> Run query
              </Button>
              <Button
                size="sm"
                variant="outline"
                className="h-7 gap-1.5 text-xs"
                onClick={onCancel}
              >
                <X className="h-3 w-3" /> Cancel
              </Button>
            </div>
          </div>
          {message.sql && (
            <SQLBlock
              sql={message.sql}
              nlQuery={message.sourceQuery}
              saved={isQuerySaved}
              onToggleSave={onToggleSaveQuery}
            />
          )}
        </div>
      </div>
    );
  }

  if (message.status === "error") {
    return (
      <div className="flex items-start gap-3">
        <AssistantAvatar />
        <div className="min-w-0 flex-1 space-y-3">
          <div className="rounded-2xl rounded-tl-sm border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm">
            <div className="mb-1 flex items-center gap-2">
              <Badge variant="destructive" className="border-0">
                <AlertCircle className="mr-1 h-3 w-3" /> Query failed
              </Badge>
              <span className="text-xs text-muted-foreground">Execution error</span>
            </div>
            <div className="font-mono text-xs text-destructive">
              {message.error ?? "Something went wrong."}
            </div>
            <div className="mt-2 text-xs text-muted-foreground">
              DB Buddy uses deterministic planning — this error was caught during validation. Try
              rephrasing your question or check your schema.
            </div>
            <div className="mt-3 flex flex-wrap gap-2">
              <Button
                size="sm"
                variant="outline"
                className="h-7 gap-1.5 text-xs"
                onClick={onRegenerate}
              >
                <RefreshCw className="h-3 w-3" /> Regenerate query
              </Button>
              <Button size="sm" variant="outline" className="h-7 gap-1.5 text-xs" onClick={onEdit}>
                <Pencil className="h-3 w-3" /> Edit question
              </Button>
            </div>
          </div>
          {message.sql && (
            <SQLBlock
              sql={message.sql}
              nlQuery={message.sourceQuery}
              saved={isQuerySaved}
              onToggleSave={onToggleSaveQuery}
            />
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="flex items-start gap-3">
      <AssistantAvatar />
      <div className="min-w-0 flex-1 space-y-3">
        <div className="rounded-2xl rounded-tl-sm border border-border bg-card px-4 py-3 shadow-[var(--shadow-soft)]">
          <div className="mb-2 flex items-center gap-2 flex-wrap">
            <StatusBadge status={message.status} />
            {message.autoFixed && (
              <Badge className="border-0 bg-amber-500/15 text-amber-400 hover:bg-amber-500/15 gap-1">
                <RefreshCw className="h-3 w-3" /> Auto-fixed
              </Badge>
            )}
            {message.aiProvider && (
              <Badge variant="outline" className="gap-1 text-[10px] font-mono">
                <Sparkles className="h-2.5 w-2.5" /> {message.aiProvider}
              </Badge>
            )}
            {message.confidence && (
              <Badge
                variant="outline"
                className={cn(
                  "text-[10px]",
                  message.confidence === "high" &&
                    "border-[oklch(0.72_0.17_155)]/40 text-[oklch(0.82_0.17_155)]",
                  message.confidence === "medium" && "border-amber-500/40 text-amber-400",
                  message.confidence === "low" && "border-destructive/40 text-destructive",
                )}
              >
                {message.confidence} confidence
              </Badge>
            )}
            {/* One high-signal execution badge: mode + roughly how fast.
                Detailed per-stage timings live under "Technical details". */}
            {message.result && (
              <Badge
                variant="outline"
                className={cn(
                  "gap-1 text-[10px]",
                  message.mode === "ai"
                    ? "border-[oklch(0.72_0.17_155)]/40 text-[oklch(0.82_0.17_155)]"
                    : "border-amber-500/40 text-amber-400",
                )}
              >
                {message.mode === "ai" ? (
                  <Sparkles className="h-2.5 w-2.5" />
                ) : (
                  <Zap className="h-2.5 w-2.5" />
                )}
                {message.mode === "ai" ? "AI-enhanced" : "Deterministic"} · {message.result.timeMs}
                ms
              </Badge>
            )}
            <span className="text-xs text-muted-foreground">
              {message.result?.rows.length} rows · {message.result?.source}
            </span>
          </div>
          {message.headline && (
            <p className="mb-2 font-display text-lg font-semibold leading-snug tracking-tight">
              {message.headline}
            </p>
          )}
          <p className="text-sm leading-relaxed text-muted-foreground">{message.text}</p>

          {/* Failure transparency - warnings and suggestions */}
          {message.warning && (
            <div className="mt-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2">
              <div className="flex items-start gap-2">
                <AlertCircle className="h-3.5 w-3.5 text-amber-400 mt-0.5 shrink-0" />
                <div className="text-xs text-amber-200">
                  <div className="font-medium">Warning</div>
                  <div className="mt-0.5 text-amber-200/80">{message.warning}</div>
                </div>
              </div>
            </div>
          )}

          {/* System intelligence visibility - stage timings */}
          {message.meta?.stage_timings && (
            <div className="mt-2">
              <Collapsible>
                <CollapsibleTrigger className="flex items-center gap-2 text-[10px] font-medium uppercase tracking-wider text-muted-foreground hover:text-foreground transition-colors">
                  <ChevronRight className="h-3 w-3 transition-transform group-data-[state=open]:rotate-90" />
                  Technical details
                </CollapsibleTrigger>
                <CollapsibleContent className="mt-2 space-y-2">
                  <div className="rounded-md border border-border/60 bg-background/50 px-3 py-2">
                    <div className="text-[10px] text-muted-foreground mb-2">
                      Pipeline Stage Timings
                    </div>
                    <div className="grid grid-cols-2 gap-2">
                      {Object.entries(message.meta.stage_timings).map(([stage, timing]) => (
                        <div key={stage} className="flex items-center justify-between text-xs">
                          <span className="text-muted-foreground">{stage}</span>
                          <span className="font-mono">{timing}ms</span>
                        </div>
                      ))}
                    </div>
                  </div>
                  {message.meta.compiler_version && (
                    <div className="text-[10px] text-muted-foreground">
                      Compiler: {message.meta.compiler_version}
                    </div>
                  )}
                </CollapsibleContent>
              </Collapsible>
            </div>
          )}

          {/* Phase 7.1: Explainability Engine */}
          {message.explanation && <ExplanationCard explanation={message.explanation} />}

          {/* Enhanced result card with structured sections */}
          {message.result && (
            <div className="mt-3 space-y-3 border-t border-border/60 pt-3">
              {/* Understanding Section */}
              {message.result.semantic?.length ? (
                <div>
                  <div className="mb-1.5 flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                    <Sparkles className="h-3 w-3" />
                    Understanding
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {message.result.semantic.map((s, si) => (
                      <span
                        key={`${message.id}-${si}-${s.from}-${s.to}`}
                        className="inline-flex items-center gap-1 rounded-md border border-border bg-background/50 px-2 py-0.5 font-mono text-[10.5px]"
                      >
                        <span className="text-muted-foreground">{s.from}</span>
                        <ArrowRight className="h-2.5 w-2.5 text-primary" />
                        <span>{s.to}</span>
                      </span>
                    ))}
                  </div>
                </div>
              ) : null}

              {/* Term Interpretation Section */}
              {message.result.termInterpretations &&
                message.result.termInterpretations.length > 0 && (
                  <TermInterpretationSection
                    interpretations={message.result.termInterpretations}
                    messageId={message.id}
                  />
                )}

              {/* Metadata Grid */}
              <div className="grid grid-cols-3 gap-2">
                <div className="rounded-md border border-border bg-background/50 px-2 py-1.5">
                  <div className="text-[9px] text-muted-foreground">Rows</div>
                  <div className="text-xs font-medium">{message.result.rows.length}</div>
                </div>
                <div className="rounded-md border border-border bg-background/50 px-2 py-1.5">
                  <div className="text-[9px] text-muted-foreground">Time</div>
                  <div className="text-xs font-medium">{message.result.timeMs}ms</div>
                </div>
                <div className="rounded-md border border-border bg-background/50 px-2 py-1.5">
                  <div className="text-[9px] text-muted-foreground">Source</div>
                  <div className="text-xs font-medium">{message.result.source}</div>
                </div>
              </div>
            </div>
          )}
        </div>

        <SQLBlock
          sql={message.sql}
          nlQuery={message.sourceQuery}
          saved={isQuerySaved}
          onToggleSave={onToggleSaveQuery}
        />

        {message.result && (
          <ResultsView
            result={message.result}
            messageId={message.id}
            sql={message.sql}
            title={message.sourceQuery ?? message.sql}
            nlQuery={message.sourceQuery}
            onSaveChart={onSaveChart}
            canSaveChart={canSaveChart}
            connectionId={connectionId}
            canViewInsights={canViewInsights}
          />
        )}
      </div>
    </div>
  );
}

// ---------- Explanation Card Component ----------
function ExplanationCard({ explanation }: { explanation: Explanation }) {
  const [open, setOpen] = useState(false);

  return (
    <div className="mt-3 p-4 rounded-xl bg-zinc-900 border border-zinc-700 text-sm transition-all duration-300 ease-in-out hover:border-zinc-600">
      <h3 className="text-lg font-semibold mb-3 flex items-center gap-2">
        <Sparkles className="h-4 w-4 text-primary" />
        How DB Buddy understood your query
      </h3>

      <p className="text-zinc-200 mb-3">
        {explanation.interpretation.aggregation && explanation.interpretation.metric ? (
          <>
            Calculated{" "}
            <span className="text-green-400 font-medium">
              {explanation.interpretation.aggregation}
            </span>{" "}
            of{" "}
            <span className="text-blue-400 font-medium">{explanation.interpretation.metric}</span>
            {explanation.interpretation.grouping.length > 0 && (
              <span> grouped by {explanation.interpretation.grouping.join(", ")}</span>
            )}
          </>
        ) : (
          // Non-aggregation queries (plain SELECT): use the backend's summary
          // ("Retrieved users.email") instead of the agg-only template, which
          // would otherwise render a broken "Calculated  of ".
          explanation.summary
        )}
      </p>

      <p className="text-xs text-zinc-500 mb-3">
        Deterministic reasoning based on your schema and learned patterns
      </p>

      <div className="space-y-1 text-zinc-400">
        {explanation.interpretation.metric && (
          <p>
            <span className="text-white">Metric:</span> {explanation.interpretation.metric}
          </p>
        )}
        {explanation.interpretation.aggregation && (
          <p>
            <span className="text-white">Aggregation:</span>{" "}
            {explanation.interpretation.aggregation}
          </p>
        )}
        {explanation.interpretation.grouping.length > 0 && (
          <p>
            <span className="text-white">Grouping:</span>{" "}
            {explanation.interpretation.grouping.join(", ")}
          </p>
        )}
      </div>

      {explanation.memory_usage.length > 0 && (
        <div className="mt-3 p-3 bg-zinc-800 rounded-lg border border-zinc-700">
          <p className="text-xs text-zinc-400 mb-1">Learned mappings</p>
          {explanation.memory_usage.map((m, i) => (
            <p key={i} className="text-xs">
              <span className="text-green-400">
                {m.term} → {m.mapped_to}
              </span>
              {m.used && <span className="text-zinc-500 ml-2">(used)</span>}
            </p>
          ))}
        </div>
      )}

      {explanation.reasoning.length > 0 && (
        <details className="mt-3 text-xs text-zinc-400">
          <summary className="cursor-pointer text-zinc-300 hover:text-foreground transition-colors">
            Show reasoning
          </summary>
          <ul className="mt-2 list-disc list-inside space-y-1">
            {explanation.reasoning.map((step, i) => (
              <li key={i}>{step}</li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

// ---------- Term Interpretation Section ----------
function TermInterpretationSection({
  interpretations,
  messageId,
}: {
  interpretations: { term: string; mapped_to: string; type: string }[];
  messageId: string;
}) {
  const [open, setOpen] = useState(false);

  function typeIcon(type: string) {
    if (type === "table") return <TableIcon className="h-3 w-3 text-primary" />;
    if (type === "column") return <Columns className="h-3 w-3 text-blue-400" />;
    return <ArrowLeftRight className="h-3 w-3 text-amber-400" />;
  }

  function typeLabel(type: string) {
    if (type === "table") return "(table)";
    if (type === "column") return "(column)";
    return "(semantic)";
  }

  function typeBadgeClass(type: string) {
    if (type === "table") return "border-primary/30 text-primary";
    if (type === "column") return "border-blue-400/30 text-blue-400";
    return "border-amber-400/30 text-amber-400";
  }

  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger asChild>
        <button className="flex w-full items-center gap-1.5 text-[10px] font-medium uppercase tracking-wider text-muted-foreground hover:text-foreground transition-colors">
          {open ? (
            <ChevronDown className="h-3 w-3 shrink-0" />
          ) : (
            <ChevronRight className="h-3 w-3 shrink-0" />
          )}
          <Sparkles className="h-3 w-3" />
          How DB Buddy interpreted your query
        </button>
      </CollapsibleTrigger>
      <CollapsibleContent>
        <div className="mt-2 flex flex-col gap-1">
          {interpretations.map((interp, i) => (
            <div
              key={`${messageId}-interp-${i}`}
              className="flex items-center gap-1.5 rounded-md border border-border bg-background/50 px-2 py-1 font-mono text-[10.5px]"
            >
              {typeIcon(interp.type)}
              <span className="text-muted-foreground">{interp.term}</span>
              <ArrowRight className="h-2.5 w-2.5 text-muted-foreground/60 shrink-0" />
              <span className="text-foreground/80">{interp.mapped_to}</span>
              <Badge
                variant="outline"
                className={cn(
                  "ml-auto h-4 px-1 text-[9px] leading-none",
                  typeBadgeClass(interp.type),
                )}
              >
                {typeLabel(interp.type)}
              </Badge>
            </div>
          ))}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}

function AssistantAvatar() {
  return (
    <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg brand-gradient">
      <Sparkles className="h-4 w-4 text-primary-foreground" />
    </div>
  );
}

function StatusBadge({
  status,
}: {
  status: "success" | "error" | "loading" | "pending_confirmation";
}) {
  if (status === "success")
    return (
      <Badge className="border-0 bg-[oklch(0.72_0.17_155)]/15 text-[oklch(0.82_0.17_155)] hover:bg-[oklch(0.72_0.17_155)]/15">
        <Check className="mr-1 h-3 w-3" />
        Success
      </Badge>
    );
  if (status === "pending_confirmation")
    return (
      <Badge className="border-0 bg-amber-500/20 text-amber-400">
        <AlertCircle className="mr-1 h-3 w-3" /> Pending
      </Badge>
    );
  return (
    <Badge variant="destructive" className="border-0">
      <AlertCircle className="mr-1 h-3 w-3" /> Error
    </Badge>
  );
}

// ---------- SQL Block ----------
function SQLBlock({
  sql,
  nlQuery,
  saved,
  onToggleSave,
}: {
  sql: string;
  nlQuery?: string;
  saved: boolean;
  onToggleSave: (nlQuery: string, sql: string) => void;
}) {
  const [open, setOpen] = useState(true);
  const [copied, setCopied] = useState(false);

  function copy() {
    navigator.clipboard.writeText(sql);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }

  return (
    <div className="overflow-hidden rounded-xl border border-border bg-card">
      <div
        role="button"
        tabIndex={0}
        onClick={() => setOpen(!open)}
        onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && setOpen(!open)}
        className="flex w-full cursor-pointer items-center justify-between px-4 py-2.5 text-xs"
      >
        <div className="flex items-center gap-2 font-medium">
          {open ? (
            <ChevronDown className="h-3.5 w-3.5" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5" />
          )}
          <Braces className="h-3.5 w-3.5 text-primary" />
          Generated SQL
        </div>
        <div className="flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
          <Button size="sm" variant="ghost" className="h-7 gap-1 px-2 text-xs" onClick={copy}>
            {copied ? (
              <Check className="h-3 w-3 text-[oklch(0.72_0.17_155)]" />
            ) : (
              <Copy className="h-3 w-3" />
            )}
            {copied ? "Copied" : "Copy"}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            className="h-7 gap-1 px-2 text-xs"
            disabled={!nlQuery}
            onClick={() => nlQuery && onToggleSave(nlQuery, sql)}
            title={
              nlQuery
                ? saved
                  ? "Remove from saved queries"
                  : "Save this query to history"
                : "Cannot save this query"
            }
          >
            <Bookmark className={cn("h-3 w-3", saved && "fill-current text-primary")} />
            {saved ? "Saved" : "Save"}
          </Button>
        </div>
      </div>
      {open && (
        <pre className="overflow-x-auto border-t border-border bg-background/60 px-4 py-3 font-mono text-[12px] leading-relaxed">
          <code className="text-foreground/90">{sql}</code>
        </pre>
      )}
    </div>
  );
}

// ---------- Results View ----------
// ---------- Shared chart + stats helpers ----------

// Reusable bar chart used in the Chart tab and Infographics.
function SimpleBarChart({ columns, rows }: { columns: string[]; rows: (string | number)[][] }) {
  const data = rows.map((r) => {
    const obj: Record<string, string | number> = {};
    columns.forEach((c, i) => (obj[c] = r[i]));
    return obj;
  });
  const numericKey = columns.find((_, i) => typeof rows[0]?.[i] === "number") ?? columns[1];
  const labelKey = columns[0];

  if (!numericKey) {
    return (
      <div className="flex h-full items-center justify-center text-xs text-muted-foreground">
        No numeric column to plot.
      </div>
    );
  }

  return (
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="oklch(1 0 0 / 0.06)" />
        <XAxis dataKey={labelKey} stroke="oklch(0.7 0.02 270)" fontSize={11} />
        <YAxis stroke="oklch(0.7 0.02 270)" fontSize={11} />
        <Tooltip
          contentStyle={{
            background: "oklch(0.205 0.022 270)",
            border: "1px solid oklch(1 0 0 / 0.1)",
            borderRadius: 8,
            fontSize: 12,
          }}
        />
        <Bar dataKey={numericKey} fill="oklch(0.65 0.21 275)" radius={[6, 6, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}

function ResultsView({
  result,
  messageId,
  sql,
  title,
  nlQuery,
  onSaveChart,
  canSaveChart,
  connectionId,
  canViewInsights,
}: {
  result: QueryResult;
  messageId: string;
  sql: string;
  title: string;
  nlQuery?: string;
  onSaveChart: (opts: {
    sql: string;
    title: string;
    nlQuery?: string;
    columns: string[];
    chartType?: SavedChart["chartType"];
  }) => void;
  canSaveChart: boolean;
  connectionId: string | null;
  canViewInsights: boolean;
}) {
  const [page, setPage] = useState(0);
  const pageSize = 10;
  const pages = Math.max(1, Math.ceil(result.rows.length / pageSize));
  const visible = result.rows.slice(page * pageSize, page * pageSize + pageSize);

  const chartData = result.rows.map((r) => {
    const obj: Record<string, string | number> = {};
    result.columns.forEach((c, i) => (obj[c] = r[i]));
    return obj;
  });

  const saveBtn = (
    <Button
      size="sm"
      variant="outline"
      className="h-7 gap-1.5 text-xs"
      disabled={!canSaveChart || !sql}
      onClick={() =>
        onSaveChart({ sql, title, nlQuery, columns: result.columns, chartType: "bar" })
      }
      title={canSaveChart ? "Save this chart to Infographics" : "Connect a database to save charts"}
    >
      <Bookmark className="h-3 w-3" /> Save chart
    </Button>
  );

  return (
    <div className="overflow-hidden rounded-xl border border-border bg-card">
      <Tabs defaultValue="table">
        <div className="flex items-center justify-between border-b border-border px-3 py-2">
          <TabsList className="bg-background/60">
            <TabsTrigger value="table" className="gap-1.5 text-xs">
              <TableIcon className="h-3.5 w-3.5" /> Table
            </TabsTrigger>
            <TabsTrigger value="json" className="gap-1.5 text-xs">
              <Braces className="h-3.5 w-3.5" /> JSON
            </TabsTrigger>
            <TabsTrigger value="chart" className="gap-1.5 text-xs">
              <BarChart3 className="h-3.5 w-3.5" /> Chart
            </TabsTrigger>
            {canViewInsights && (
              <TabsTrigger value="insights" className="gap-1.5 text-xs">
                <Sparkles className="h-3.5 w-3.5" /> Insights
              </TabsTrigger>
            )}
          </TabsList>
          <div className="text-xs text-muted-foreground">{result.rows.length} rows</div>
        </div>

        <TabsContent value="table" className="m-0">
          <div className="max-h-80 overflow-auto">
            <Table>
              <TableHeader>
                <TableRow className="hover:bg-transparent">
                  {result.columns.map((c) => (
                    <TableHead
                      key={`${messageId}-${c}`}
                      className="font-mono text-[11px] uppercase tracking-wider text-muted-foreground"
                    >
                      {c}
                    </TableHead>
                  ))}
                </TableRow>
              </TableHeader>
              <TableBody>
                {visible.map((row, i) => (
                  <TableRow key={i} className="border-border/50">
                    {row.map((cell, j) => (
                      <TableCell key={j} className="font-mono text-xs">
                        {typeof cell === "number" ? cell.toLocaleString() : cell}
                      </TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
          {pages > 1 && (
            <div className="flex items-center justify-between border-t border-border px-3 py-2 text-xs text-muted-foreground">
              <span>
                Page {page + 1} of {pages}
              </span>
              <div className="flex gap-1">
                <Button
                  size="sm"
                  variant="outline"
                  className="h-7 text-xs"
                  disabled={page === 0}
                  onClick={() => setPage(page - 1)}
                >
                  Prev
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  className="h-7 text-xs"
                  disabled={page >= pages - 1}
                  onClick={() => setPage(page + 1)}
                >
                  Next
                </Button>
              </div>
            </div>
          )}
        </TabsContent>

        <TabsContent value="json" className="m-0">
          <pre className="max-h-80 overflow-auto bg-background/60 p-4 font-mono text-[11px] leading-relaxed">
            <code>{JSON.stringify(chartData, null, 2)}</code>
          </pre>
        </TabsContent>

        <TabsContent value="chart" className="m-0 p-4">
          <div className="mb-2 flex justify-end">{saveBtn}</div>
          <div className="h-72 w-full">
            <SimpleBarChart columns={result.columns} rows={result.rows} />
          </div>
        </TabsContent>

        {canViewInsights && (
          <TabsContent value="insights" className="m-0">
            <InsightsPanel
              columns={result.columns}
              rows={result.rows}
              sql={sql}
              question={nlQuery}
              connectionId={connectionId}
              database={result.source}
            />
          </TabsContent>
        )}
      </Tabs>
    </div>
  );
}

// ---------- Composer ----------
function Composer({
  value,
  setValue,
  onSend,
  sending,
  hasDb,
}: {
  value: string;
  setValue: (s: string) => void;
  onSend: () => void;
  sending: boolean;
  hasDb: boolean;
}) {
  return (
    <div className="border-t border-border/60 bg-background/80 px-4 sm:px-8 py-4 backdrop-blur">
      <div className="mx-auto max-w-4xl">
        <div
          className={cn(
            "group relative rounded-2xl border bg-card shadow-[var(--shadow-soft)] transition-colors",
            hasDb ? "border-border focus-within:border-primary/60" : "border-border/40 opacity-60",
          )}
        >
          <Textarea
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                onSend();
              }
            }}
            rows={1}
            disabled={!hasDb}
            placeholder={
              hasDb
                ? "Ask your database (e.g., 'total sales last month')"
                : "Connect a database to start asking questions…"
            }
            className="min-h-[56px] resize-none border-0 bg-transparent px-4 py-4 pr-14 text-sm shadow-none focus-visible:ring-0 disabled:cursor-not-allowed"
          />
          <div className="absolute bottom-2 right-2 flex items-center gap-1">
            <Button
              size="icon"
              onClick={onSend}
              disabled={!!(sending || !value.trim() || !hasDb)}
              className="h-9 w-9 rounded-full brand-gradient text-primary-foreground hover:opacity-90 disabled:opacity-40"
              aria-label="Send"
            >
              {sending ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Send className="h-4 w-4" />
              )}
            </Button>
          </div>
        </div>
        <p className="mt-2 text-center text-[11px] text-muted-foreground">
          DB Buddy can make mistakes. Always review generated SQL before running in production.
        </p>
      </div>
    </div>
  );
}

// ---------- Right panel ----------
// Sequential steps shown by the transient AnalyzeProgress indicator. Each has a
// present-continuous "active" label (shown while in progress) and a done label.
const ANALYZE_STEPS = [
  { done: "Connected", active: "Connecting…" },
  { done: "Schema analyzed", active: "Analyzing schema…" },
  { done: "Semantic layer ready", active: "Building semantic layer…" },
  { done: "Vector index ready", active: "Indexing vectors…" },
  { done: "Context cached", active: "Caching context…" },
];

// A transient, loading-style progress indicator shown ONLY while the schema is
// being analyzed in the background. It animates through the steps and then
// smoothly fades/collapses away once the semantic layer is ready — it is not a
// persistent panel (that information isn't needed once analysis is done).
function AnalyzeProgress({
  dbName,
  analysisLoading,
}: {
  dbName?: string;
  analysisLoading: boolean;
}) {
  const [phase, setPhase] = useState(1); // 0=Connected is done the instant we start
  const [stage, setStage] = useState<"hidden" | "running" | "finishing">("hidden");
  const [show, setShow] = useState(false); // drives the fade/collapse
  const prevLoading = useRef(false);

  useEffect(() => {
    let interval: ReturnType<typeof setInterval> | undefined;
    let fadeT: ReturnType<typeof setTimeout> | undefined;
    let hideT: ReturnType<typeof setTimeout> | undefined;

    if (analysisLoading) {
      // Start the animated sequence. Phase advances on a timer (the backend
      // builds atomically, so this paces a smooth reveal) and never auto-
      // completes the last step until the real work actually finishes.
      setStage("running");
      setShow(true);
      setPhase(1);
      interval = setInterval(() => {
        setPhase((p) => Math.min(p + 1, ANALYZE_STEPS.length - 1));
      }, 850);
    } else if (prevLoading.current) {
      // Work just finished: fill every step, hold briefly, then fade + collapse.
      setStage("finishing");
      setPhase(ANALYZE_STEPS.length);
      fadeT = setTimeout(() => setShow(false), 1000);
      hideT = setTimeout(() => setStage("hidden"), 1550);
    }
    prevLoading.current = analysisLoading;

    return () => {
      if (interval) clearInterval(interval);
      if (fadeT) clearTimeout(fadeT);
      if (hideT) clearTimeout(hideT);
    };
  }, [analysisLoading]);

  if (stage === "hidden") return null;

  return (
    <div
      className={cn(
        "mx-5 mt-5 overflow-hidden rounded-2xl border border-border bg-background/70 transition-all duration-500 ease-out",
        show
          ? "max-h-72 p-4 opacity-100 translate-y-0"
          : "max-h-0 border-transparent p-0 opacity-0 -translate-y-1",
      )}
    >
      <div className="text-[10px] uppercase tracking-[0.28em] text-muted-foreground">Preparing</div>
      <h3 className="mb-3 text-base font-semibold tracking-tight text-foreground">
        {dbName ?? "your database"}
      </h3>
      <div className="space-y-2">
        {ANALYZE_STEPS.map((step, i) => {
          const isDone = i < phase;
          const isActive = i === phase && stage === "running";
          return (
            <div
              key={step.done}
              className={cn(
                "flex items-center gap-2 text-xs transition-all duration-500 ease-out",
                isDone || isActive ? "translate-x-0 opacity-100" : "translate-x-1 opacity-40",
              )}
            >
              {isDone ? (
                <Check className="h-3.5 w-3.5 text-[oklch(0.72_0.17_155)]" />
              ) : isActive ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin text-accent" />
              ) : (
                <CircleDot className="h-3.5 w-3.5 text-muted-foreground/40" />
              )}
              <span
                className={cn(
                  isDone ? "text-foreground" : isActive ? "text-accent" : "text-muted-foreground",
                )}
              >
                {isActive ? step.active : step.done}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function RightPanel({
  result,
  resultMessageId,
  analysisResult,
  analysisError,
  hasConnectedDb,
  onConnect,
  activeDb,
  analysisLoading,
}: {
  result?: QueryResult;
  resultMessageId?: string;
  analysisResult?: SemanticAnalysisResult | null;
  analysisError?: string | null;
  hasConnectedDb: boolean;
  onConnect: () => void;
  activeDb: DB | null;
  analysisLoading: boolean;
}) {
  const [search, setSearch] = useState("");

  const grouped = useMemo(() => {
    if (!analysisResult?.semantic_layer) return {} as Record<string, SemanticColumn[]>;

    const groupedMap: Record<string, SemanticColumn[]> = {};

    Object.entries(analysisResult.semantic_layer).forEach(([tableName, columns]) => {
      Object.entries(columns).forEach(([column, entry]) => {
        const term = entry.term || "Unknown";
        if (!groupedMap[term]) groupedMap[term] = [];
        groupedMap[term].push({
          column,
          table: tableName,
          source: entry.source,
          provider: entry.provider,
          plugin: entry.plugin,
        });
      });
    });

    return groupedMap;
  }, [analysisResult]);

  const totalColumnsAnalyzed = useMemo(
    () =>
      Object.values(analysisResult?.semantic_layer ?? {}).reduce(
        (sum, table) => sum + Object.keys(table).length,
        0,
      ),
    [analysisResult],
  );

  const sortedGroups = useMemo(
    () => Object.entries(grouped).sort((a, b) => b[1].length - a[1].length),
    [grouped],
  );

  const filteredGroups = useMemo(() => {
    const term = search.trim().toLowerCase();
    if (!term) return sortedGroups;

    return sortedGroups
      .map(([groupTerm, columns]) => {
        // A match on the term name keeps the whole group; otherwise narrow down
        // to the columns that match by column name, table, or "table.column" —
        // so searching a specific column or table actually filters the list.
        if (groupTerm.toLowerCase().includes(term)) {
          return [groupTerm, columns] as [string, SemanticColumn[]];
        }
        const matching = columns.filter((item) => {
          const table = (item.table ?? "").toLowerCase();
          const column = item.column.toLowerCase();
          return (
            column.includes(term) || table.includes(term) || `${table}.${column}`.includes(term)
          );
        });
        return [groupTerm, matching] as [string, SemanticColumn[]];
      })
      .filter(([, columns]) => columns.length > 0);
  }, [sortedGroups, search]);

  function downloadJSON() {
    if (!analysisResult) return;
    const blob = new Blob([JSON.stringify(analysisResult, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "semantic_layer.json";
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <aside className="hidden w-96 shrink-0 flex-col border-l border-border bg-card/30 xl:flex">
      <div className="border-b border-border px-5 py-4">
        <div className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
          Query Metadata
        </div>
      </div>
      {activeDb && <AnalyzeProgress dbName={activeDb.database} analysisLoading={analysisLoading} />}
      {analysisResult ? (
        <div className="flex flex-1 flex-col gap-5 overflow-y-auto px-5 py-5 text-sm">
          <div className="rounded-2xl border border-border bg-background/70 p-4">
            <div className="mb-3 flex items-center justify-between gap-2">
              <div>
                <div className="text-[10px] uppercase tracking-[0.28em] text-muted-foreground">
                  Semantic Layer
                </div>
                <h3 className="text-base font-semibold tracking-tight text-foreground">
                  Grouped intelligence
                </h3>
              </div>
              <Button
                size="sm"
                variant="outline"
                className="h-8 gap-1.5 text-xs"
                onClick={downloadJSON}
              >
                <Download className="h-3.5 w-3.5" /> Export
              </Button>
            </div>

            <div className="relative mt-3">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search terms or columns"
                className="h-9 bg-card pl-9 text-xs"
              />
            </div>

            {analysisResult.metadata.ai_used ? (
              <div className="mt-3 inline-flex items-center gap-2 rounded-full bg-primary/10 px-3 py-1 text-[11px] font-medium text-primary">
                <Sparkles className="h-3.5 w-3.5" /> AI Mapping Active
                {analysisResult.metadata.ai_providers_used?.length
                  ? ` (${analysisResult.metadata.ai_providers_used.join(", ")})`
                  : ""}
              </div>
            ) : analysisResult.metadata.ai_requested ? (
              <div className="mt-3 flex items-start gap-2 rounded-lg bg-amber-500/10 px-3 py-2 text-[11px] font-medium text-amber-400">
                <AlertCircle className="h-3.5 w-3.5 mt-0.5 shrink-0" />
                <span>
                  Rule-based fallback — AI provider unavailable
                  {analysisResult.metadata.ai_error ? (
                    <span className="mt-0.5 block font-normal text-amber-200/80">
                      {analysisResult.metadata.ai_error}
                    </span>
                  ) : null}
                </span>
              </div>
            ) : null}

            <div className="mt-4 grid grid-cols-2 gap-2 text-xs">
              <StatCard label="Columns analyzed" value={totalColumnsAnalyzed.toLocaleString()} />
              <StatCard label="Unique terms" value={sortedGroups.length.toLocaleString()} />
              <StatCard label="AI usage" value={analysisResult.metadata.ai_used ? "Yes" : "No"} />
              <StatCard label="Top term" value={sortedGroups[0]?.[0] ?? "—"} />
            </div>
            <div className="mt-3 flex flex-wrap gap-2">
              {sortedGroups.slice(0, 5).map(([term, columns]) => (
                <span
                  key={term}
                  className="rounded-full border border-border bg-card/80 px-2.5 py-1 text-[11px] text-muted-foreground"
                >
                  {term} <strong className="text-foreground">({columns.length})</strong>
                </span>
              ))}
            </div>
          </div>

          <div className="space-y-3">
            {filteredGroups.length ? (
              filteredGroups.map(([term, columns], index) => (
                <div
                  key={term}
                  style={{
                    animation: "fadeIn 220ms ease-out both",
                    animationDelay: `${index * 45}ms`,
                  }}
                >
                  <SemanticGroup term={term} columns={columns} />
                </div>
              ))
            ) : (
              <div className="rounded-2xl border border-dashed border-border bg-background/60 p-4 text-xs text-muted-foreground">
                No semantic groups match your search.
              </div>
            )}
          </div>

          <div className="rounded-2xl border border-border bg-background/70 p-4 text-xs">
            <div className="text-[10px] uppercase tracking-[0.28em] text-muted-foreground">
              Metadata
            </div>
            <div className="mt-3 space-y-2">
              <div className="flex items-center justify-between">
                <span>Database</span>
                <strong>{analysisResult.metadata.database}</strong>
              </div>
              <div className="flex items-center justify-between">
                <span>AI used</span>
                <strong>{analysisResult.metadata.ai_used ? "Yes" : "No"}</strong>
              </div>
            </div>
          </div>
        </div>
      ) : result ? (
        <div className="flex flex-col gap-5 px-5 py-5 text-sm">
          <Metric icon={Clock} label="Time taken" value={`${result.timeMs} ms`} />
          <Metric icon={Rows} label="Rows returned" value={result.rows.length.toLocaleString()} />
          <Metric icon={Database} label="Data source" value={result.source} />

          {/* Understanding Layer */}
          {((result.termInterpretations && result.termInterpretations.length > 0) ||
            result.relevanceReason) && (
            <div className="flex flex-col gap-3 border-t border-border pt-4">
              <div className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
                Understanding Layer
              </div>

              {result.termInterpretations && result.termInterpretations.length > 0 && (
                <div className="flex flex-col gap-1.5">
                  {result.termInterpretations.map((ti, index) => (
                    <div
                      key={index}
                      className="flex items-center justify-between rounded-md border border-border bg-background/50 px-3 py-2 font-mono text-xs"
                    >
                      <span className="text-muted-foreground">{ti.term}</span>
                      <ChevronRight className="h-3.5 w-3.5 text-muted-foreground" />
                      <span className="text-foreground">{ti.mapped_to}</span>
                    </div>
                  ))}
                </div>
              )}

              {result.relevanceReason && (
                <div className="rounded-md border border-border bg-background/30 p-2.5 text-xs text-muted-foreground">
                  <div className="font-semibold text-foreground/80 mb-1">Relevance Reasoning</div>
                  <div>{result.relevanceReason}</div>
                </div>
              )}
            </div>
          )}

          <div>
            <div className="mb-2 text-xs font-medium uppercase tracking-wider text-muted-foreground">
              Semantic interpretation
            </div>
            <div className="flex flex-col gap-1.5">
              {result.semantic.map((s, si) => (
                <div
                  key={`${resultMessageId ?? `rp-sem-${si}`}-${si}-${s.from}-${s.to}`}
                  className="flex items-center justify-between rounded-md border border-border bg-background/50 px-3 py-2 font-mono text-xs"
                >
                  <span className="text-muted-foreground">{s.from}</span>
                  <ChevronRight className="h-3 w-3 text-muted-foreground" />
                  <span className="text-foreground">{s.to}</span>
                </div>
              ))}
            </div>
          </div>

          <div>
            <div className="mb-2 text-xs font-medium uppercase tracking-wider text-muted-foreground">
              Columns
            </div>
            <div className="flex flex-wrap gap-1.5">
              {result.columns.map((c, ci) => (
                <span
                  key={`${resultMessageId ?? `rp-col-${ci}`}-${ci}-${c}`}
                  className="rounded-md border border-border bg-background/50 px-2 py-1 font-mono text-[11px]"
                >
                  {c}
                </span>
              ))}
            </div>
          </div>
        </div>
      ) : (
        <div className="flex flex-1 items-center justify-center px-6 text-center text-xs text-muted-foreground">
          {analysisError ? (
            <div className="rounded-2xl border border-amber-500/40 bg-amber-500/10 p-5 text-left shadow-[var(--shadow-soft)]">
              <div className="flex items-center gap-2 text-amber-500">
                <AlertTriangle className="h-4 w-4 shrink-0" />
                <div className="text-[10px] font-medium uppercase tracking-[0.28em]">
                  Analysis unavailable
                </div>
              </div>
              <p className="mt-2 text-xs leading-relaxed text-muted-foreground">{analysisError}</p>
            </div>
          ) : hasConnectedDb ? (
            "Click Analyze Schema to fetch the semantic layer and metadata from the backend."
          ) : (
            <div className="rounded-2xl border border-dashed border-border bg-background/70 p-5 text-left shadow-[var(--shadow-soft)]">
              <div className="text-[10px] uppercase tracking-[0.28em] text-muted-foreground">
                Ready when you are
              </div>
              <h3 className="mt-1 text-sm font-semibold text-foreground">
                Connect a database and click “Analyze Schema” to begin
              </h3>
              <p className="mt-2 text-xs text-muted-foreground">
                The semantic layer and grouped intelligence will appear here once your schema is
                analyzed.
              </p>
              <Button
                size="sm"
                className="mt-4 brand-gradient text-primary-foreground"
                onClick={onConnect}
              >
                Connect a database
              </Button>
            </div>
          )}
        </div>
      )}
    </aside>
  );
}

function StatCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl border border-border bg-card/80 p-3 shadow-[var(--shadow-soft)]">
      <div className="text-[10px] uppercase tracking-[0.24em] text-muted-foreground">{label}</div>
      <div className="mt-1 text-base font-semibold text-foreground">{value}</div>
    </div>
  );
}

function Metric({
  icon: Icon,
  label,
  value,
}: {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  value: string;
}) {
  return (
    <div className="flex items-center justify-between">
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <Icon className="h-3.5 w-3.5" />
        {label}
      </div>
      <div className="font-mono text-sm">{value}</div>
    </div>
  );
}

// ---------- Query History Panel ----------
function QueryHistoryPanel({
  history,
  hasDb,
  onRerun,
  onClear,
  onDelete,
  onLoadQuery,
}: {
  history: HistoryItem[];
  hasDb: boolean;
  onRerun: (query: string) => void;
  onClear: () => void;
  onDelete: (id: string) => void;
  onLoadQuery: (query: string) => void;
}) {
  const [confirmClear, setConfirmClear] = useState(false);

  // History is already most-recent-first, scoped to the active database.
  const queryHistory = history.map((h) => ({
    id: h.id,
    userQuery: h.query,
    status: h.status,
    confidence: h.confidence,
    pinned: h.pinned,
  }));
  const hasUnpinned = history.some((h) => !h.pinned);

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-y-auto px-8 py-6">
      <div className="mb-6 flex items-start justify-between gap-4">
        <div>
          <h1 className="font-display text-2xl font-semibold tracking-tight">Query History</h1>
          <p className="mt-1 text-sm text-muted-foreground">View and re-run your past queries</p>
        </div>
        {hasUnpinned && (
          <div className="flex flex-col items-end gap-1.5">
            {confirmClear ? (
              <div className="flex items-center gap-2">
                <Button
                  size="sm"
                  variant="destructive"
                  className="h-8 gap-1.5 text-xs"
                  onClick={() => {
                    onClear();
                    setConfirmClear(false);
                  }}
                >
                  <Trash2 className="h-3 w-3" /> Confirm clear
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-8 text-xs"
                  onClick={() => setConfirmClear(false)}
                >
                  Cancel
                </Button>
              </div>
            ) : (
              <Button
                size="sm"
                variant="outline"
                className="h-8 gap-1.5 text-xs text-destructive hover:text-destructive"
                onClick={() => setConfirmClear(true)}
              >
                <Trash2 className="h-3 w-3" /> Clear history
              </Button>
            )}
            <p className="text-[11px] text-muted-foreground/70">
              Clears unsaved queries only · saved queries stay until you delete them.
            </p>
          </div>
        )}
      </div>

      {queryHistory.length === 0 ? (
        <div className="flex flex-1 items-center justify-center">
          <div className="text-center">
            <History className="mx-auto h-12 w-12 text-muted-foreground/50" />
            <p className="mt-4 text-sm text-muted-foreground">
              {hasDb ? "No query history yet" : "Connect a database to see its query history"}
            </p>
          </div>
        </div>
      ) : (
        <div className="flex-1 overflow-y-auto">
          <div className="space-y-3">
            {queryHistory.map((item) => (
              <div
                key={item.id}
                className="rounded-xl border border-border bg-card/80 p-4 hover:border-primary/60 transition-colors"
              >
                <div className="flex items-start justify-between gap-4">
                  <div className="flex-1">
                    <p className="text-sm font-medium">{item.userQuery}</p>
                    <div className="mt-2 flex items-center gap-2">
                      {item.pinned && (
                        <Badge className="border-0 bg-primary/15 text-primary">
                          <Bookmark className="mr-1 h-3 w-3 fill-current" /> Saved
                        </Badge>
                      )}
                      {item.status === "success" && (
                        <Badge className="border-0 bg-[oklch(0.72_0.17_155)]/15 text-[oklch(0.82_0.17_155)]">
                          <Check className="mr-1 h-3 w-3" /> Success
                        </Badge>
                      )}
                      {item.status === "error" && (
                        <Badge variant="destructive" className="border-0">
                          <AlertCircle className="mr-1 h-3 w-3" /> Failed
                        </Badge>
                      )}
                      {item.confidence && (
                        <Badge
                          variant="outline"
                          className={cn(
                            "text-[10px]",
                            item.confidence === "high" &&
                              "border-[oklch(0.72_0.17_155)]/40 text-[oklch(0.82_0.17_155)]",
                            item.confidence === "medium" && "border-amber-500/40 text-amber-400",
                            item.confidence === "low" && "border-destructive/40 text-destructive",
                          )}
                        >
                          {item.confidence} confidence
                        </Badge>
                      )}
                    </div>
                  </div>
                  <div className="flex gap-2">
                    <Button
                      size="sm"
                      variant="outline"
                      className="h-8 gap-1.5 text-xs"
                      onClick={() => onRerun(item.userQuery)}
                    >
                      <RefreshCw className="h-3 w-3" /> Re-run
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      className="h-8 gap-1.5 text-xs"
                      onClick={() => onLoadQuery(item.userQuery)}
                    >
                      <Pencil className="h-3 w-3" /> Edit
                    </Button>
                    <Button
                      size="icon"
                      variant="ghost"
                      className="h-8 w-8 text-destructive hover:text-destructive"
                      onClick={() => onDelete(item.id)}
                      aria-label="Delete"
                      title={item.pinned ? "Delete saved query" : "Delete from history"}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ---------- Infographics (saved charts) ----------

// Turn a raw DB error into a likely cause, using the schema fingerprint saved
// with the chart to distinguish a schema change from permissions/other issues.
function classifyChartFailure(message: string): string {
  const m = message.toLowerCase();
  if (/permission|denied|not allowed|privilege/.test(m)) {
    return "You may no longer have permission to run this query.";
  }
  if (
    /does not exist|unknown column|unknown table|no such table|undefined column|undefined table|relation .* does not exist/.test(
      m,
    )
  ) {
    return "A table or column this chart depends on was changed or removed.";
  }
  return message;
}

function SavedChartCard({
  chart,
  db,
  onDelete,
  onPublish,
  onUnpublish,
  onUpdateChart,
  canPublish,
}: {
  chart: SavedChart;
  db: DB | undefined;
  onDelete: () => void;
  onPublish: () => void;
  onUnpublish: () => void;
  onUpdateChart: (
    id: string,
    patch: { chartType?: ChartType; config?: ChartConfig | null },
  ) => void;
  canPublish: boolean;
}) {
  const isPublished = chart.status === "published";
  const [open, setOpen] = useState(false);
  const [customizing, setCustomizing] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [data, setData] = useState<{ columns: string[]; rows: (string | number)[][] } | null>(null);
  // True when the live query succeeded but the columns differ from when saved.
  const [schemaChanged, setSchemaChanged] = useState(false);

  // Re-run the saved SQL against the live database. Nothing is cached — every
  // open reflects the current state of the data (and any external changes).
  async function runLive() {
    if (!db) {
      setError("The database for this chart is not connected.");
      setData(null);
      return;
    }
    setLoading(true);
    setError(null);
    setSchemaChanged(false);
    setData(null);
    try {
      const res = await apiFetch("/execute", {
        method: "POST",
        body: JSON.stringify({ connection_id: db.id, sql: chart.sql }),
      });
      const json = await readJson<ExecuteApiResponse>(res);
      if (!res.ok)
        throw new Error(
          json?.detail || `Query failed (HTTP ${res.status}). Is the backend running?`,
        );
      if (!json) throw new Error("The backend returned an empty response.");
      const rawRows: Record<string, unknown>[] = json.results ?? [];
      if (!rawRows.length) {
        setData({ columns: [], rows: [] });
        return;
      }
      const columns = Object.keys(rawRows[0]);
      const rows = rawRows.map((r) => columns.map((c) => r[c] as string | number));
      // The query still runs, but if its columns changed since the chart was
      // saved, flag it so the user knows the visualization may differ.
      if (chart.schemaFingerprint && columnFingerprint(columns) !== chart.schemaFingerprint) {
        setSchemaChanged(true);
      }
      setData({ columns, rows });
    } catch (e) {
      setError(classifyChartFailure(e instanceof Error ? e.message : "Failed to load chart."));
      setData(null);
    } finally {
      setLoading(false);
    }
  }

  function toggle() {
    const next = !open;
    setOpen(next);
    if (next) runLive(); // refresh from the live DB every time it is opened
  }

  return (
    <div className="rounded-xl border border-border bg-card/80">
      <div className="flex items-center justify-between gap-3 p-4">
        <button className="flex min-w-0 flex-1 items-center gap-2 text-left" onClick={toggle}>
          {open ? (
            <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" />
          ) : (
            <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" />
          )}
          <div className="min-w-0">
            <p className="flex items-center gap-2 truncate text-sm font-medium">
              {chart.title}
              {isPublished && (
                <Badge variant="secondary" className="gap-1 text-[10px] uppercase tracking-wide">
                  <CheckCircle2 className="h-3 w-3 text-green-500" /> Published
                </Badge>
              )}
            </p>
            <p className="truncate text-[11px] text-muted-foreground">
              {db?.name ?? "Unlinked database"} · saved{" "}
              {new Date(chart.createdAt).toLocaleDateString()}
            </p>
          </div>
        </button>
        <div className="flex items-center gap-1">
          {open && (
            <Button
              size="icon"
              variant="ghost"
              className="h-8 w-8"
              onClick={runLive}
              aria-label="Refresh"
              title="Refresh from database"
            >
              <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
            </Button>
          )}
          {open && (
            <Button
              size="sm"
              variant={customizing ? "secondary" : "ghost"}
              className="h-8 gap-1.5 text-xs"
              onClick={() => {
                if (!open) setOpen(true);
                setCustomizing((v) => !v);
              }}
              title="Change chart type and colors"
            >
              <Palette className="h-3.5 w-3.5" /> Customize
            </Button>
          )}
          <PinToDashboard chartId={chart.id} chartTitle={chart.title} />
          {canPublish &&
            (isPublished ? (
              <Button
                size="sm"
                variant="ghost"
                className="h-8 gap-1.5 text-xs"
                onClick={onUnpublish}
                title="Revoke client access"
              >
                Unpublish
              </Button>
            ) : (
              <Button
                size="sm"
                variant="outline"
                className="h-8 gap-1.5 text-xs"
                onClick={onPublish}
                title="Publish to your organization"
              >
                <ArrowRight className="h-3 w-3" /> Publish
              </Button>
            ))}
          <Button
            size="icon"
            variant="ghost"
            className="h-8 w-8 text-destructive hover:text-destructive"
            onClick={onDelete}
            aria-label="Delete chart"
            title="Delete chart"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        </div>
      </div>

      {open && (
        <div className="border-t border-border p-4">
          {loading ? (
            <div className="flex h-60 items-center justify-center text-xs text-muted-foreground">
              <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading latest data…
            </div>
          ) : error || !db ? (
            <div className="flex flex-col items-center justify-center gap-2 py-8 text-center">
              <AlertCircle className="h-6 w-6 text-amber-400" />
              <p className="text-sm font-medium text-amber-400">Chart needs attention</p>
              <p className="text-xs text-muted-foreground">
                {error ?? "The database for this chart is not connected."}
              </p>
              <code className="mt-1 max-w-full truncate rounded bg-background/60 px-2 py-1 font-mono text-[10px] text-muted-foreground">
                {chart.sql}
              </code>
            </div>
          ) : data && data.rows.length ? (
            <>
              {schemaChanged && (
                <div className="mb-2 flex items-center gap-2 rounded-md bg-amber-500/10 px-3 py-1.5 text-[11px] text-amber-400">
                  <AlertCircle className="h-3.5 w-3.5 shrink-0" />
                  The result columns changed since this chart was saved — the visualization may
                  differ.
                </div>
              )}
              {customizing && (
                <ChartCustomizer
                  chart={chart}
                  columns={data.columns}
                  rows={data.rows}
                  onUpdateChart={onUpdateChart}
                />
              )}
              <ChartRenderer
                columns={data.columns}
                rows={data.rows}
                type={chart.chartType}
                config={chart.config}
                height={260}
              />
              <div className="mt-2 flex items-center justify-between gap-2">
                <p className="truncate text-[11px] text-muted-foreground">
                  {data.rows.length} row(s) · live from {db?.name ?? "database"}
                </p>
                {chart.nlQuery && (
                  <p
                    className="truncate text-[11px] italic text-muted-foreground/70"
                    title={chart.nlQuery}
                  >
                    “{chart.nlQuery}”
                  </p>
                )}
              </div>
            </>
          ) : (
            <div className="flex h-32 items-center justify-center text-xs text-muted-foreground">
              Query returned no rows.
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// Chart customization controls shown in an expanded Infographics card: pick a
// chart type, a base palette, and override the color of each series or each
// category (bar / pie slice). Every change persists via onUpdateChart.
function ChartCustomizer({
  chart,
  columns,
  rows,
  onUpdateChart,
}: {
  chart: SavedChart;
  columns: string[];
  rows: (string | number)[][];
  onUpdateChart: (
    id: string,
    patch: { chartType?: ChartType; config?: ChartConfig | null },
  ) => void;
}) {
  const config: ChartConfig = chart.config ?? {};
  const targets = colorTargets(chart.chartType, columns, rows);
  const palette = PALETTES[config.palette ?? "default"] ?? PALETTES.default;

  const shownColor = (key: string, i: number) => {
    const bucket = targets.mode === "series" ? config.seriesColors : config.categoryColors;
    return bucket?.[key] ?? palette[i % palette.length];
  };

  function setElementColor(key: string, hex: string) {
    const bucketKey = targets.mode === "series" ? "seriesColors" : "categoryColors";
    onUpdateChart(chart.id, {
      config: { ...config, [bucketKey]: { ...(config[bucketKey] ?? {}), [key]: hex } },
    });
  }

  // Cap the pickers so a high-cardinality category axis doesn't render hundreds
  // of inputs; the palette still colors the rest.
  const MAX_PICKERS = 24;
  const shownKeys = targets.keys.slice(0, MAX_PICKERS);

  return (
    <div className="mb-3 rounded-lg border border-border bg-background/40 p-3">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
        {/* Chart type */}
        <label className="flex items-center gap-2 text-xs text-muted-foreground">
          <span className="font-medium text-foreground">Type</span>
          <select
            className="h-8 rounded-md border border-border bg-background px-2 text-xs text-foreground"
            value={chart.chartType}
            onChange={(e) => onUpdateChart(chart.id, { chartType: e.target.value as ChartType })}
          >
            {CHART_TYPES.map((t) => (
              <option key={t.value} value={t.value}>
                {t.label}
              </option>
            ))}
          </select>
        </label>

        {/* Palette */}
        <div className="flex items-center gap-2">
          <span className="text-xs font-medium">Palette</span>
          <div className="flex items-center gap-1.5">
            {Object.entries(PALETTES).map(([name, colors]) => {
              const active = (config.palette ?? "default") === name;
              return (
                <button
                  key={name}
                  type="button"
                  title={name}
                  onClick={() => onUpdateChart(chart.id, { config: { ...config, palette: name } })}
                  className={cn(
                    "flex h-6 items-center overflow-hidden rounded border",
                    active ? "border-primary ring-1 ring-primary" : "border-border",
                  )}
                >
                  {colors.slice(0, 4).map((c) => (
                    <span key={c} className="h-full w-2" style={{ backgroundColor: c }} />
                  ))}
                </button>
              );
            })}
          </div>
        </div>

        {(config.seriesColors || config.categoryColors) && (
          <button
            type="button"
            className="text-xs text-muted-foreground underline underline-offset-2 hover:text-foreground"
            onClick={() => onUpdateChart(chart.id, { config: { palette: config.palette } })}
          >
            Reset colors
          </button>
        )}
      </div>

      {/* Per-element colors */}
      {targets.mode === "none" ? (
        <p className="mt-3 text-[11px] text-muted-foreground">
          This chart type uses a single color; pick a palette above.
        </p>
      ) : (
        <div className="mt-3">
          <p className="mb-1.5 text-[11px] font-medium text-muted-foreground">
            {targets.mode === "series" ? "Series colors" : "Category colors"}
          </p>
          <div className="flex flex-wrap gap-2">
            {shownKeys.map((key, i) => (
              <label
                key={key}
                className="flex items-center gap-1.5 rounded-md border border-border bg-background/60 px-2 py-1 text-[11px]"
                title={key}
              >
                <input
                  type="color"
                  value={toHex(shownColor(key, i))}
                  onChange={(e) => setElementColor(key, e.target.value)}
                  className="h-4 w-4 cursor-pointer border-0 bg-transparent p-0"
                />
                <span className="max-w-[9rem] truncate">{key || "(blank)"}</span>
              </label>
            ))}
          </div>
          {targets.keys.length > MAX_PICKERS && (
            <p className="mt-1.5 text-[11px] text-muted-foreground/70">
              Showing the first {MAX_PICKERS} of {targets.keys.length}; the palette colors the rest.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

// <input type="color"> requires a 7-char hex; coerce named/short values safely.
function toHex(c: string): string {
  if (/^#[0-9a-fA-F]{6}$/.test(c)) return c;
  if (/^#[0-9a-fA-F]{3}$/.test(c)) {
    return (
      "#" +
      c
        .slice(1)
        .split("")
        .map((ch) => ch + ch)
        .join("")
    );
  }
  return "#6366f1";
}

function InfographicsPanel({
  charts,
  databases,
  onDelete,
  onDeleteAll,
  onPublish,
  onUnpublish,
  onUpdateChart,
  canPublish,
  canViewRelations,
}: {
  charts: SavedChart[];
  databases: DB[];
  onDelete: (id: string) => void;
  onDeleteAll: () => void;
  onPublish: (id: string) => void;
  onUnpublish: (id: string) => void;
  onUpdateChart: (
    id: string,
    patch: { chartType?: ChartType; config?: ChartConfig | null },
  ) => void;
  canPublish: boolean;
  // The relation graph is analyst-only (gated on schema:analyze server-side); the
  // Relations sub-tab is only shown when the caller holds it.
  canViewRelations: boolean;
}) {
  const [confirmAll, setConfirmAll] = useState(false);

  const dbById = useMemo(() => {
    const map: Record<string, DB> = {};
    databases.forEach((d) => {
      map[d.id] = d;
    });
    return map;
  }, [databases]);

  const deleteAllControl =
    charts.length === 0 ? null : confirmAll ? (
      <div className="flex items-center gap-2">
        <Button
          size="sm"
          variant="destructive"
          className="h-8 gap-1.5 text-xs"
          onClick={() => {
            onDeleteAll();
            setConfirmAll(false);
          }}
        >
          <Trash2 className="h-3 w-3" /> Confirm delete all
        </Button>
        <Button
          size="sm"
          variant="ghost"
          className="h-8 text-xs"
          onClick={() => setConfirmAll(false)}
        >
          Cancel
        </Button>
      </div>
    ) : (
      <Button
        size="sm"
        variant="outline"
        className="h-8 gap-1.5 text-xs text-destructive hover:text-destructive"
        onClick={() => setConfirmAll(true)}
      >
        <Trash2 className="h-3 w-3" /> Delete all charts
      </Button>
    );

  const chartsBody = (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
      {deleteAllControl && <div className="mb-3 flex justify-end">{deleteAllControl}</div>}
      {charts.length === 0 ? (
        <div className="flex flex-1 items-center justify-center">
          <div className="text-center">
            <LineChart className="mx-auto h-12 w-12 text-muted-foreground/50" />
            <p className="mt-4 text-sm text-muted-foreground">No saved charts yet</p>
            <p className="mt-1 text-xs text-muted-foreground/70">
              Run a query, open the Chart tab, and click “Save chart”.
            </p>
          </div>
        </div>
      ) : (
        <div className="space-y-3">
          {charts.map((c) => (
            <SavedChartCard
              key={c.id}
              chart={c}
              db={c.databaseConnectionId ? dbById[c.databaseConnectionId] : undefined}
              onDelete={() => onDelete(c.id)}
              onPublish={() => onPublish(c.id)}
              onUnpublish={() => onUnpublish(c.id)}
              onUpdateChart={onUpdateChart}
              canPublish={canPublish}
            />
          ))}
        </div>
      )}
    </div>
  );

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col px-8 py-6">
      <div className="mb-6">
        <h1 className="font-display text-2xl font-semibold tracking-tight">Infographics</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          {canViewRelations
            ? "Saved charts, dashboards, and how your databases and tables interconnect."
            : "Saved charts and dashboards — everything re-runs live when you open it."}
        </p>
      </div>

      <Tabs defaultValue="charts" className="flex min-h-0 flex-1 flex-col">
        <TabsList>
          <TabsTrigger value="charts" className="gap-1.5 text-xs">
            <LineChart className="h-3.5 w-3.5" /> Charts
          </TabsTrigger>
          <TabsTrigger value="dashboards" className="gap-1.5 text-xs">
            <LayoutDashboard className="h-3.5 w-3.5" /> Dashboards
          </TabsTrigger>
          {canViewRelations && (
            <TabsTrigger value="relations" className="gap-1.5 text-xs">
              <Network className="h-3.5 w-3.5" /> Relations
            </TabsTrigger>
          )}
        </TabsList>
        <TabsContent value="charts" className="mt-4 flex min-h-0 flex-1 flex-col">
          {chartsBody}
        </TabsContent>
        <TabsContent value="dashboards" className="mt-4 flex min-h-0 flex-1 flex-col">
          <DashboardsPanel canAuthor canPublish={canPublish} />
        </TabsContent>
        {canViewRelations && (
          <TabsContent value="relations" className="mt-4 flex min-h-0 flex-1 flex-col">
            <RelationGraph />
          </TabsContent>
        )}
      </Tabs>
    </div>
  );
}

// ---------- Settings Panel ----------
function SettingsPanel({
  theme,
  toggleTheme,
  onProviderActivated,
  autoExecuteReads,
  setAutoExecuteReads,
  onBack,
  onClearAllConnections,
}: {
  theme: "dark" | "light";
  toggleTheme: () => void;
  onProviderActivated: () => void;
  autoExecuteReads: boolean;
  setAutoExecuteReads: (v: boolean) => void;
  onBack: () => void;
  onClearAllConnections: () => Promise<void>;
}) {
  const [clearing, setClearing] = useState(false);

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-y-auto px-8 py-6">
      <div className="mb-6 flex items-center gap-4">
        <Button variant="ghost" size="sm" onClick={onBack} className="gap-2">
          <ArrowRight className="h-4 w-4 rotate-180" /> Back
        </Button>
        <div>
          <h1 className="font-display text-2xl font-semibold tracking-tight">Settings</h1>
          <p className="mt-1 text-sm text-muted-foreground">Configure your DB Buddy experience</p>
        </div>
      </div>

      <div className="max-w-2xl space-y-6">
        {/* AI Providers — one page manages every provider as a record. */}
        <AIProviders onActiveChange={onProviderActivated} />

        {/* Execution Settings */}
        <div className="rounded-xl border border-border bg-card/80 p-6">
          <h2 className="mb-4 text-sm font-semibold">Execution Settings</h2>
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <div>
                <div className="text-sm font-medium">Auto-execute READ queries</div>
                <div className="text-xs text-muted-foreground">
                  Automatically run SELECT queries without confirmation
                </div>
              </div>
              <Button
                size="sm"
                variant={autoExecuteReads ? "default" : "outline"}
                onClick={() => setAutoExecuteReads(!autoExecuteReads)}
                className="w-[80px]"
              >
                {autoExecuteReads ? "On" : "Off"}
              </Button>
            </div>
          </div>
        </div>

        {/* Security */}
        <div className="rounded-xl border border-border bg-card/80 p-6">
          <h2 className="mb-4 text-sm font-semibold">Security</h2>
          <MfaCard />
        </div>

        {/* Personal API Keys (CLI & automation) */}
        <div className="rounded-xl border border-border bg-card/80 p-6">
          <h2 className="mb-4 text-sm font-semibold">Personal API Keys</h2>
          <PersonalApiKeys />
        </div>

        {/* Appearance */}
        <div className="rounded-xl border border-border bg-card/80 p-6">
          <h2 className="mb-4 text-sm font-semibold">Appearance</h2>
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <div>
                <div className="text-sm font-medium">Theme</div>
                <div className="text-xs text-muted-foreground">
                  Choose your preferred color scheme
                </div>
              </div>
              <Button size="sm" variant="outline" onClick={toggleTheme} className="w-[140px] gap-2">
                {theme === "dark" ? <Moon className="h-4 w-4" /> : <Sun className="h-4 w-4" />}
                {theme === "dark" ? "Dark" : "Light"}
              </Button>
            </div>
          </div>
        </div>

        {/* Database Management */}
        <div className="rounded-xl border border-border bg-card/80 p-6">
          <h2 className="mb-4 text-sm font-semibold">Database Management</h2>
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <div>
                <div className="text-sm font-medium">Disconnect all databases</div>
                <div className="text-xs text-muted-foreground">
                  Remove every saved connection from your account. Saved charts and query history
                  that referenced them are kept but unlinked.
                </div>
              </div>
              <Button
                size="sm"
                variant="destructive"
                disabled={clearing}
                onClick={async () => {
                  setClearing(true);
                  try {
                    await onClearAllConnections();
                  } finally {
                    setClearing(false);
                  }
                }}
              >
                {clearing ? "Clearing…" : "Clear"}
              </Button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------- Connect DB modal ----------
function ConnectDatabaseModal({
  open,
  editing,
  onOpenChange,
  onConnect,
  onUpdate,
}: {
  open: boolean;
  editing?: DB | null;
  onOpenChange: (b: boolean) => void;
  onConnect: (fields: {
    name: string;
    engine: string;
    host: string;
    user: string;
    password: string;
    database: string;
    port: number | null;
  }) => Promise<void>;
  onUpdate: (
    id: string,
    fields: {
      name: string;
      engine: string;
      host: string;
      user: string;
      password: string;
      database: string;
      port: number | null;
    },
  ) => Promise<void>;
}) {
  const isEditing = !!editing;
  // A stale connection (undecryptable stored secret) must be given a new password.
  const passwordRequired = isEditing && editing?.credentialsOk === false;

  const [name, setName] = useState("");
  const [engine, setEngine] = useState("MySQL");
  const [host, setHost] = useState("127.0.0.1");
  const [user, setUser] = useState("root");
  const [password, setPassword] = useState("");
  const [database, setDatabase] = useState("testdb");
  const [port, setPort] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // When the dialog opens, prefill from the connection being edited (or reset to
  // sensible defaults for a brand-new connection). Password always starts empty:
  // in edit mode an empty value means "keep the existing password".
  useEffect(() => {
    if (!open) return;
    setError(null);
    setPassword("");
    if (editing) {
      setName(editing.name);
      setEngine(editing.engine);
      setHost(editing.host);
      setUser(editing.user);
      setDatabase(editing.database);
      setPort("");
    } else {
      setName("");
      setEngine("MySQL");
      setHost("127.0.0.1");
      setUser("root");
      setDatabase("testdb");
      setPort("");
    }
  }, [open, editing]);

  async function submit() {
    if (!name.trim()) return;
    if (passwordRequired && !password.trim()) {
      setError("Enter the password — the stored one can no longer be decrypted.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const fields = {
        name,
        engine,
        host,
        user,
        password,
        database,
        port: port.trim() ? Number(port) : null,
      };
      if (editing) {
        await onUpdate(editing.id, fields);
      } else {
        await onConnect(fields);
      }
      onOpenChange(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save the connection.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="bg-card">
        <DialogHeader>
          <DialogTitle>{isEditing ? "Edit connection" : "Connect a database"}</DialogTitle>
          <DialogDescription>
            {isEditing
              ? "Update this connection. Leave the password blank to keep the existing one."
              : "Add a new database connection. Credentials are encrypted and never leave your workspace."}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div>
            <label className="mb-1 block text-xs text-muted-foreground">Connection name</label>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="my_database"
            />
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted-foreground">Engine</label>
            <Select value={engine} onValueChange={setEngine}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {["MySQL", "PostgreSQL", "SQL Server"].map((e) => (
                  <SelectItem key={e} value={e}>
                    {e}
                  </SelectItem>
                ))}
                {["BigQuery", "Snowflake", "ClickHouse", "Redshift"].map((e) => (
                  <SelectItem key={e} value={e} disabled>
                    {e} (coming soon)
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted-foreground">Host</label>
            <Input value={host} onChange={(e) => setHost(e.target.value)} placeholder="localhost" />
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted-foreground">Port</label>
            <Input
              type="number"
              value={port}
              onChange={(e) => setPort(e.target.value)}
              placeholder="Default for engine"
            />
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted-foreground">User</label>
            <Input value={user} onChange={(e) => setUser(e.target.value)} placeholder="root" />
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted-foreground">
              Password{passwordRequired && <span className="text-destructive"> (required)</span>}
            </label>
            <Input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder={isEditing ? "Leave blank to keep existing password" : "••••••••"}
            />
            {passwordRequired && (
              <p className="mt-1 text-[11px] text-amber-500">
                Stored credentials can no longer be decrypted. Please enter the password again.
              </p>
            )}
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted-foreground">Database</label>
            <Input
              value={database}
              onChange={(e) => setDatabase(e.target.value)}
              placeholder="testdb"
            />
          </div>
        </div>
        {error && <p className="text-xs text-destructive">{error}</p>}
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={busy}>
            Cancel
          </Button>
          <Button
            className="gap-2 brand-gradient text-primary-foreground"
            onClick={submit}
            disabled={busy || !name.trim()}
          >
            {busy && <Loader2 className="h-4 w-4 animate-spin" />}
            {isEditing ? "Save changes" : "Connect"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

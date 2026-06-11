import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useMemo, useRef, useState } from "react";import {
  MessageSquare, History, Database, Settings, Plus, Send, Mic, Copy, Bookmark,
  Check, ChevronDown, ChevronRight, Sparkles, Table as TableIcon, BarChart3,
  Braces, Sun, Moon, Clock, Rows, CircleDot, Loader2, AlertCircle, X,
  RefreshCw, Pencil, ArrowRight, Download, Search,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "@/components/ui/table";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
} from "recharts";
import { cn } from "@/lib/utils";
import { SemanticGroup, type SemanticColumn } from "@/components/SemanticGroup";

export const Route = createFileRoute("/app")({
  component: AppShell,
});

// Render nothing on the server — the app is fully client-driven with no SSR
// value. This eliminates all hydration mismatches from state-dependent props
// (disabled, placeholder, title) that differ between server and client.
function AppShell() {
  const [mounted, setMounted] = useState(false);
  useEffect(() => { setMounted(true); }, []);
  if (!mounted) return <AppLoadingSkeleton />;
  return <DBBuddyApp />;
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
type DB = {
  id: string;
  name: string;
  engine: string;
  status: DBStatus;
  host: string;
  user: string;
  password: string;
  database: string;
};

type SemanticAnalysisResult = {
  semantic_layer: Record<string, Record<string, { term: string; source?: string; provider?: string; plugin?: string }>>;
  metadata: { database: string; ai_used: boolean };
};

type QueryResult = {
  columns: string[];
  rows: (string | number)[][];
  timeMs: number;
  source: string;
  semantic: { from: string; to: string }[];
};

type Message =
  | { id: string; role: "user"; text: string }
  | {
      id: string;
      role: "assistant";
      text: string;
      headline?: string;
      sql: string;
      status: "success" | "error" | "loading";
      result?: QueryResult;
      error?: string;
      sourceQuery?: string;
      autoFixed?: boolean;
      confidence?: "high" | "medium" | "low";
      aiProvider?: string;
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
  const [theme, setTheme] = useState<"dark" | "light">("dark");
  const [section, setSection] = useState<"chat" | "history" | "databases" | "settings">("chat");
  const [databases, setDatabases] = useState<DB[]>(initialDatabases);
  const [activeDb, setActiveDb] = useState<string>("");
  const [connectOpen, setConnectOpen] = useState(false);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [analysisLoading, setAnalysisLoading] = useState(false);
  const [analysisResult, setAnalysisResult] = useState<SemanticAnalysisResult | null>(null);
  const [analysisError, setAnalysisError] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Derive query history from real messages — deduplicated, most recent first
  const queryHistory = useMemo(
    () => [
      ...new Map(
        messages
          .filter((m) => m.role === "user")
          .map((m) => [m.text, m.text])
      ).values(),
    ]
      .slice(-10)
      .reverse(),
    [messages],
  );

  // Active DB object
  const activeDbObj = useMemo(
    () => databases.find((d) => d.name === activeDb) ?? null,
    [databases, activeDb],
  );

  useEffect(() => {
    const root = document.documentElement;
    root.classList.toggle("light", theme === "light");
    root.classList.toggle("dark", theme === "dark");
  }, [theme]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  // Auto-analyze when active DB changes (only if it has credentials)
  useEffect(() => {
    if (activeDbObj && activeDbObj.host && activeDbObj.user && activeDbObj.database) {
      analyzeDB(activeDbObj);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeDb]);

  const lastAssistant = useMemo(
    () => [...messages].reverse().find((m): m is Extract<Message, { role: "assistant" }> => m.role === "assistant" && m.status === "success"),
    [messages],
  );

  async function ask(text: string) {
    if (!text.trim() || sending) return;

    const db = activeDbObj;
    if (!db) {
      setConnectOpen(true);
      return;
    }

    const userMsg: Message = { id: crypto.randomUUID(), role: "user", text };
    const loadingId = crypto.randomUUID();
    const loadingMsg: Message = {
      id: loadingId, role: "assistant", text: "", sql: "", status: "loading", sourceQuery: text,
    };
    setMessages((m) => [...m, userMsg, loadingMsg]);
    setInput("");
    setSending(true);

    try {
      const startMs = Date.now();
      const res = await fetch("http://127.0.0.1:8000/query", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          host: db.host,
          user: db.user,
          password: db.password,
          database: db.database,
          question: text,
          ai: true,
          ai_provider: "hybrid",
        }),
      });

      const data = await res.json();

      if (!res.ok) {
        throw new Error(data?.detail || "Query failed.");
      }

      const elapsedMs = Date.now() - startMs;

      // Non-SELECT: generated but held for approval
      if (!data.auto_executed) {
        setMessages((m) => m.map((msg) =>
          msg.id === loadingId
            ? {
                ...msg,
                status: "error" as const,
                sql: data.sql ?? "",
                text: "",
                error: data.warning ?? "This query may modify data. Review the SQL before executing.",
                confidence: data.confidence ?? "medium",
                aiProvider: db.engine,
              }
            : msg,
        ));
        return;
      }

      // SELECT with results
      const rawRows: Record<string, unknown>[] = data.results ?? [];
      const columns = rawRows.length > 0 ? Object.keys(rawRows[0]) : [];
      const rows = rawRows.map((r) => columns.map((c) => r[c] as string | number));

      // Build semantic interpretation from semantic_layer
      const semanticLayer: Record<string, Record<string, { term: string }>> =
        data.semantic_layer ?? {};
      const semantic: { from: string; to: string }[] = columns
        .map((col) => {
          for (const table of Object.values(semanticLayer)) {
            if (table[col]) return { from: table[col].term, to: col };
          }
          return null;
        })
        .filter(Boolean) as { from: string; to: string }[];

      setMessages((m) => m.map((msg) =>
        msg.id === loadingId
          ? {
              ...msg,
              status: "success" as const,
              sql: data.sql ?? "",
              text: data.auto_fixed
                ? "Query was automatically repaired and re-executed."
                : `Here are the results for: "${text}"`,
              result: { columns, rows, timeMs: elapsedMs, source: db.name, semantic },
              confidence: data.confidence ?? "high",
              autoFixed: data.auto_fixed ?? false,
              aiProvider: "hybrid",
            }
          : msg,
      ));
    } catch (err) {
      setMessages((m) => m.map((msg) =>
        msg.id === loadingId
          ? {
              ...msg,
              status: "error" as const,
              sql: "",
              text: "",
              error: err instanceof Error ? err.message : "Something went wrong.",
            }
          : msg,
      ));
    } finally {
      setSending(false);
    }
  }

  async function analyzeDB(dbOverride?: DB) {
    const db = dbOverride ?? databases.find((item) => item.name === activeDb);

    if (!db || !db.host || !db.user || !db.database) {
      setAnalysisError("Select a connected database and provide host, user, and database details first.");
      return;
    }

    setAnalysisLoading(true);
    setAnalysisError(null);

    try {
      const res = await fetch("http://127.0.0.1:8000/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          host: db.host,
          user: db.user,
          password: db.password,
          database: db.database,
          ai: true,
          ai_provider: "local",
        }),
      });

      const data = await res.json();
      if (!res.ok) {
        throw new Error(data?.detail || "Analysis request failed.");
      }

      setAnalysisResult(data as SemanticAnalysisResult);
      setSection("chat");
    } catch (error) {
      setAnalysisError(error instanceof Error ? error.message : "Unable to analyze the database schema.");
    } finally {
      setAnalysisLoading(false);
    }
  }

  function newChat() {
    setMessages([]);
    setInput("");
    setSection("chat");
  }

  function regenerate(sourceQuery?: string) {
    if (sourceQuery) ask(sourceQuery);
  }


  return (
    <div className="flex h-screen w-full overflow-hidden bg-background text-foreground">
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
        theme={theme}
        toggleTheme={() => setTheme(theme === "dark" ? "light" : "dark")}
        onNewChat={newChat}
        onRerun={ask}
        queryHistory={queryHistory}
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
          hasDb={!!activeDbObj}
        />

        <div className="relative flex min-h-0 flex-1">
          <div className="flex min-w-0 flex-1 flex-col">
            <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 sm:px-8 py-6">
              {messages.length === 0 ? (
                <EmptyState onPick={ask} hasDb={!!activeDbObj} onConnect={() => setConnectOpen(true)} />
              ) : (
                <div className="mx-auto flex max-w-4xl flex-col gap-6 pb-4">
                  {messages.map((m) =>
                    m.role === "user" ? (
                      <UserBubble key={m.id} text={m.text} />
                    ) : (
                      <AssistantBubble
                        key={m.id}
                        message={m}
                        onRegenerate={() => regenerate(m.sourceQuery)}
                        onEdit={() => m.sourceQuery && setInput(m.sourceQuery)}
                      />
                    ),
                  )}
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

          {/* Right panel */}
          <RightPanel
            result={lastAssistant?.result}
            resultMessageId={lastAssistant?.id}
            analysisResult={analysisResult}
            analysisError={analysisError}
            hasConnectedDb={databases.some((db) => db.status === "connected")}
            onConnect={() => setConnectOpen(true)}
          />
        </div>
      </main>

      <ConnectDatabaseModal
        open={connectOpen}
        onOpenChange={setConnectOpen}
        onAdd={(db) => {
          setDatabases((d) => [...d, db]);
          setActiveDb(db.name);
        }}
      />
    </div>
  );
}

// ---------- Sidebar ----------
function Sidebar(props: {
  section: string;
  setSection: (s: any) => void;
  databases: DB[];
  activeDb: string;
  setActiveDb: (s: string) => void;
  onConnect: () => void;
  theme: "dark" | "light";
  toggleTheme: () => void;
  onNewChat: () => void;
  onRerun: (q: string) => void;
  queryHistory: string[];
}) {
  const nav = [
    { id: "chat", label: "Chat", icon: MessageSquare },
    { id: "history", label: "Query History", icon: History },
    { id: "databases", label: "Databases", icon: Database },
    { id: "settings", label: "Settings", icon: Settings },
  ] as const;

  return (
    <aside className="hidden w-72 shrink-0 flex-col border-r border-sidebar-border bg-sidebar text-sidebar-foreground md:flex">
      <div className="flex items-center gap-3 px-5 py-5">
        <div className="flex h-9 w-9 items-center justify-center rounded-xl brand-gradient glow">
          <Database className="h-5 w-5 text-primary-foreground" />
        </div>
        <div>
          <div className="font-display text-lg font-semibold leading-none tracking-tight">DB Buddy</div>
          <div className="mt-1 text-[11px] text-muted-foreground">AI database assistant</div>
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
                "flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors",
                active
                  ? "bg-sidebar-accent text-sidebar-accent-foreground"
                  : "text-muted-foreground hover:bg-sidebar-accent/60 hover:text-sidebar-foreground",
              )}
            >
              <Icon className="h-4 w-4" />
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
            props.queryHistory.map((h) => (
              <button
                key={h}
                onClick={() => props.onRerun(h)}
                title={`Re-run: ${h}`}
                className="group flex items-center gap-2 truncate rounded-md px-2 py-1.5 text-left text-xs text-muted-foreground hover:bg-sidebar-accent/60 hover:text-sidebar-foreground"
              >
                <Clock className="h-3 w-3 shrink-0 opacity-60 group-hover:text-primary" />
                <span className="truncate">{h}</span>
              </button>
            ))
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
            <button
              key={db.id}
              onClick={() => db.status === "connected" && props.setActiveDb(db.name)}
              className={cn(
                "group flex items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left text-sm transition-colors",
                props.activeDb === db.name
                  ? "bg-sidebar-accent text-sidebar-accent-foreground"
                  : "hover:bg-sidebar-accent/60",
              )}
            >
              <div className="flex min-w-0 items-center gap-2">
                <CircleDot
                  className={cn(
                    "h-3 w-3 shrink-0",
                    db.status === "connected" ? "text-[oklch(0.72_0.17_155)]" : "text-muted-foreground/50",
                  )}
                />
                <div className="min-w-0">
                  <div className="truncate text-xs font-medium">{db.name}</div>
                  <div className="truncate text-[10px] text-muted-foreground">{db.engine}</div>
                </div>
              </div>
            </button>
          ))}
        </div>

        <button
          onClick={props.toggleTheme}
          className="mt-4 flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-xs text-muted-foreground hover:bg-sidebar-accent/60 hover:text-foreground"
        >
          {props.theme === "dark" ? <Sun className="h-3.5 w-3.5" /> : <Moon className="h-3.5 w-3.5" />}
          {props.theme === "dark" ? "Light theme" : "Dark theme"}
        </button>
      </div>
    </aside>
  );
}

// ---------- Top bar ----------
function TopBar({ activeDb, databases, setActiveDb, onNewChat, onAnalyze, analysisLoading, hasDb }: {
  activeDb: string;
  databases: DB[];
  setActiveDb: (s: string) => void;
  onNewChat: () => void;
  onAnalyze: () => void;
  analysisLoading: boolean;
  hasDb: boolean;
}) {
  return (
    <header className="relative z-10 flex items-center justify-between gap-4 border-b border-border/60 bg-background/80 px-4 sm:px-8 py-3 backdrop-blur">
      <div>
        <h1 className="font-display text-xl font-semibold tracking-tight sm:text-2xl">
          {activeDb
            ? <>Connected to <span className="text-brand-gradient">{activeDb}</span></>
            : <>Connect a <span className="text-brand-gradient">database</span> to start</>}
        </h1>
        <p className="text-xs text-muted-foreground">
          <Sparkles className="mr-1 inline h-3 w-3 text-accent" />
          Powered by AI + Semantic Layer
        </p>
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
          {analysisLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
          {analysisLoading ? "Analyzing…" : "Analyze Schema"}
        </Button>
        {databases.filter((d) => d.status === "connected").length > 0 ? (
          <Select value={activeDb} onValueChange={setActiveDb}>
            <SelectTrigger className="w-[200px] bg-card">
              <Database className="mr-2 h-3.5 w-3.5 text-muted-foreground" />
              <SelectValue placeholder="Select database" />
            </SelectTrigger>
            <SelectContent>
              {databases.filter((d) => d.status === "connected").map((d) => (
                <SelectItem key={d.id} value={d.name}>
                  {d.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        ) : null}
      </div>
    </header>
  );
}


// ---------- Empty state ----------
function EmptyState({ onPick, hasDb, onConnect }: {
  onPick: (s: string) => void;
  hasDb: boolean;
  onConnect: () => void;
}) {
  if (!hasDb) {
    return (
      <div className="mx-auto flex h-full max-w-md flex-col items-center justify-center gap-6 py-10 text-center">
        <div className="flex h-16 w-16 items-center justify-center rounded-2xl brand-gradient glow">
          <Database className="h-7 w-7 text-primary-foreground" />
        </div>
        <div>
          <h2 className="font-display text-2xl font-semibold tracking-tight">Connect a database</h2>
          <p className="mt-2 text-sm text-muted-foreground">
            Add your MySQL credentials to start asking questions in plain English.
          </p>
        </div>
        <Button className="gap-2 brand-gradient text-primary-foreground" onClick={onConnect}>
          <Plus className="h-4 w-4" /> Connect database
        </Button>
      </div>
    );
  }

  return (
    <div className="mx-auto flex h-full max-w-3xl flex-col items-center justify-center gap-8 py-10 text-center">
      <div className="flex h-16 w-16 items-center justify-center rounded-2xl brand-gradient glow">
        <Sparkles className="h-7 w-7 text-primary-foreground" />
      </div>
      <div>
        <h2 className="font-display text-3xl font-semibold tracking-tight">What do you want to know?</h2>
        <p className="mt-2 text-sm text-muted-foreground">
          DB Buddy turns plain English into SQL, runs it, and explains the results.
        </p>
      </div>
      <div className="grid w-full grid-cols-1 gap-2 sm:grid-cols-2">
        {suggestions.map((s) => (
          <button
            key={s}
            onClick={() => onPick(s)}
            className="group rounded-xl border border-border bg-card/60 p-4 text-left transition-all hover:border-primary/60 hover:bg-card hover:shadow-[var(--shadow-soft)]"
          >
            <div className="flex items-start gap-3">
              <div className="rounded-md bg-primary/10 p-1.5 text-primary group-hover:bg-primary/20">
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
  message, onRegenerate, onEdit,
}: {
  message: Extract<Message, { role: "assistant" }>;
  onRegenerate: () => void;
  onEdit: () => void;
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
            <div className="font-mono text-xs text-destructive">{message.error ?? "Something went wrong."}</div>
            <div className="mt-3 flex flex-wrap gap-2">
              <Button size="sm" variant="outline" className="h-7 gap-1.5 text-xs" onClick={onRegenerate}>
                <RefreshCw className="h-3 w-3" /> Regenerate query
              </Button>
              <Button size="sm" variant="outline" className="h-7 gap-1.5 text-xs" onClick={onEdit}>
                <Pencil className="h-3 w-3" /> Edit question
              </Button>
            </div>
          </div>
          {message.sql && <SQLBlock sql={message.sql} />}
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
                  message.confidence === "high" && "border-[oklch(0.72_0.17_155)]/40 text-[oklch(0.82_0.17_155)]",
                  message.confidence === "medium" && "border-amber-500/40 text-amber-400",
                  message.confidence === "low" && "border-destructive/40 text-destructive",
                )}
              >
                {message.confidence} confidence
              </Badge>
            )}
            <span className="text-xs text-muted-foreground">
              {message.result?.rows.length} rows · {message.result?.timeMs}ms · {message.result?.source}
            </span>
          </div>
          {message.headline && (
            <p className="mb-2 font-display text-lg font-semibold leading-snug tracking-tight">
              {message.headline}
            </p>
          )}
          <p className="text-sm leading-relaxed text-muted-foreground">{message.text}</p>

          {message.result?.semantic?.length ? (
            <div className="mt-3 border-t border-border/60 pt-3">
              <div className="mb-1.5 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                Semantic interpretation
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
        </div>

        <SQLBlock sql={message.sql} />


        {message.result && <ResultsView result={message.result} messageId={message.id} />}
      </div>
    </div>
  );
}

function AssistantAvatar() {
  return (
    <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg brand-gradient">
      <Sparkles className="h-4 w-4 text-primary-foreground" />
    </div>
  );
}

function StatusBadge({ status }: { status: "success" | "error" | "loading" }) {
  if (status === "success")
    return (
      <Badge className="border-0 bg-[oklch(0.72_0.17_155)]/15 text-[oklch(0.82_0.17_155)] hover:bg-[oklch(0.72_0.17_155)]/15">
        <Check className="mr-1 h-3 w-3" />
        Success
      </Badge>
    );
  return (
    <Badge variant="destructive" className="border-0">
      <AlertCircle className="mr-1 h-3 w-3" /> Error
    </Badge>
  );
}

// ---------- SQL Block ----------
function SQLBlock({ sql }: { sql: string }) {
  const [open, setOpen] = useState(true);
  const [copied, setCopied] = useState(false);
  const [saved, setSaved] = useState(false);

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
          {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
          <Braces className="h-3.5 w-3.5 text-primary" />
          Generated SQL
        </div>
        <div className="flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
          <Button size="sm" variant="ghost" className="h-7 gap-1 px-2 text-xs" onClick={copy}>
            {copied ? <Check className="h-3 w-3 text-[oklch(0.72_0.17_155)]" /> : <Copy className="h-3 w-3" />}
            {copied ? "Copied" : "Copy"}
          </Button>
          <Button size="sm" variant="ghost" className="h-7 gap-1 px-2 text-xs" onClick={() => setSaved(!saved)}>
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
function ResultsView({ result, messageId }: { result: QueryResult; messageId: string }) {
  const [page, setPage] = useState(0);
  const pageSize = 10;
  const pages = Math.max(1, Math.ceil(result.rows.length / pageSize));
  const visible = result.rows.slice(page * pageSize, page * pageSize + pageSize);

  const chartData = result.rows.map((r) => {
    const obj: Record<string, string | number> = {};
    result.columns.forEach((c, i) => (obj[c] = r[i]));
    return obj;
  });

  const numericKey = result.columns.find((_, i) => typeof result.rows[0]?.[i] === "number") ?? result.columns[1];
  const labelKey = result.columns[0];

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
          </TabsList>
          <div className="text-xs text-muted-foreground">
            {result.rows.length} rows
          </div>
        </div>

        <TabsContent value="table" className="m-0">
          <div className="max-h-80 overflow-auto">
            <Table>
              <TableHeader>
                <TableRow className="hover:bg-transparent">
                  {result.columns.map((c) => (
                    <TableHead key={`${messageId}-${c}`} className="font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
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
              <span>Page {page + 1} of {pages}</span>
              <div className="flex gap-1">
                <Button size="sm" variant="outline" className="h-7 text-xs" disabled={page === 0} onClick={() => setPage(page - 1)}>
                  Prev
                </Button>
                <Button size="sm" variant="outline" className="h-7 text-xs" disabled={page >= pages - 1} onClick={() => setPage(page + 1)}>
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
          <div className="h-72 w-full">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={chartData} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
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
          </div>
        </TabsContent>
      </Tabs>
    </div>
  );
}

// ---------- Composer ----------
function Composer({
  value, setValue, onSend, sending, hasDb,
}: { value: string; setValue: (s: string) => void; onSend: () => void; sending: boolean; hasDb: boolean }) {
  return (
    <div className="border-t border-border/60 bg-background/80 px-4 sm:px-8 py-4 backdrop-blur">
      <div className="mx-auto max-w-4xl">
        <div className={cn(
          "group relative rounded-2xl border bg-card shadow-[var(--shadow-soft)] transition-colors",
          hasDb ? "border-border focus-within:border-primary/60" : "border-border/40 opacity-60",
        )}>
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
            disabled={!!(!hasDb)}
            placeholder={hasDb ? "Ask your database (e.g., 'total sales last month')" : "Connect a database to start asking questions…"}
            className="min-h-[56px] resize-none border-0 bg-transparent px-4 py-4 pr-28 text-sm shadow-none focus-visible:ring-0 disabled:cursor-not-allowed"
          />
          <div className="absolute bottom-2 right-2 flex items-center gap-1">
            <Button size="icon" variant="ghost" className="h-9 w-9 rounded-full text-muted-foreground" aria-label="Voice (coming soon)" disabled={true}>
              <Mic className="h-4 w-4" />
            </Button>
            <Button
              size="icon"
              onClick={onSend}
              disabled={!!(sending || !value.trim() || !hasDb)}
              className="h-9 w-9 rounded-full brand-gradient text-primary-foreground hover:opacity-90 disabled:opacity-40"
              aria-label="Send"
            >
              {sending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
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
function RightPanel({
  result,
  resultMessageId,
  analysisResult,
  analysisError,
  hasConnectedDb,
  onConnect,
}: {
  result?: QueryResult;
  resultMessageId?: string;
  analysisResult?: SemanticAnalysisResult | null;
  analysisError?: string | null;
  hasConnectedDb: boolean;
  onConnect: () => void;
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
    () => Object.values(analysisResult?.semantic_layer ?? {}).reduce((sum, table) => sum + Object.keys(table).length, 0),
    [analysisResult],
  );

  const sortedGroups = useMemo(
    () => Object.entries(grouped).sort((a, b) => b[1].length - a[1].length),
    [grouped],
  );

  const filteredGroups = useMemo(() => {
    const term = search.trim().toLowerCase();
    if (!term) return sortedGroups;

    return sortedGroups.filter(([groupTerm, columns]) => {
      const haystack = `${groupTerm} ${columns.map((item) => item.column).join(" ")}`.toLowerCase();
      return haystack.includes(term);
    });
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
      {analysisResult ? (
        <div className="flex flex-1 flex-col gap-5 overflow-y-auto px-5 py-5 text-sm">
          <div className="rounded-2xl border border-border bg-background/70 p-4">
            <div className="mb-3 flex items-center justify-between gap-2">
              <div>
                <div className="text-[10px] uppercase tracking-[0.28em] text-muted-foreground">Semantic Layer</div>
                <h3 className="text-base font-semibold tracking-tight text-foreground">Grouped intelligence</h3>
              </div>
              <Button size="sm" variant="outline" className="h-8 gap-1.5 text-xs" onClick={downloadJSON}>
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
                <Sparkles className="h-3.5 w-3.5" /> AI Enhanced Mapping Enabled
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
                <span key={term} className="rounded-full border border-border bg-card/80 px-2.5 py-1 text-[11px] text-muted-foreground">
                  {term} <strong className="text-foreground">({columns.length})</strong>
                </span>
              ))}
            </div>
          </div>

          <div className="space-y-3">
            {filteredGroups.length ? filteredGroups.map(([term, columns], index) => (
              <div key={term} style={{ animation: "fadeIn 220ms ease-out both", animationDelay: `${index * 45}ms` }}>
                <SemanticGroup term={term} columns={columns} />
              </div>
            )) : (
              <div className="rounded-2xl border border-dashed border-border bg-background/60 p-4 text-xs text-muted-foreground">
                No semantic groups match your search.
              </div>
            )}
          </div>

          <div className="rounded-2xl border border-border bg-background/70 p-4 text-xs">
            <div className="text-[10px] uppercase tracking-[0.28em] text-muted-foreground">Metadata</div>
            <div className="mt-3 space-y-2">
              <div className="flex items-center justify-between"><span>Database</span><strong>{analysisResult.metadata.database}</strong></div>
              <div className="flex items-center justify-between"><span>AI used</span><strong>{analysisResult.metadata.ai_used ? "Yes" : "No"}</strong></div>
            </div>
          </div>
        </div>
      ) : result ? (
        <div className="flex flex-col gap-5 px-5 py-5 text-sm">
          <Metric icon={Clock} label="Time taken" value={`${result.timeMs} ms`} />
          <Metric icon={Rows} label="Rows returned" value={result.rows.length.toLocaleString()} />
          <Metric icon={Database} label="Data source" value={result.source} />

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
                <span key={`${resultMessageId ?? `rp-col-${ci}`}-${ci}-${c}`} className="rounded-md border border-border bg-background/50 px-2 py-1 font-mono text-[11px]">
                  {c}
                </span>
              ))}
            </div>
          </div>
        </div>
      ) : (
        <div className="flex flex-1 items-center justify-center px-6 text-center text-xs text-muted-foreground">
          {analysisError ? (
            <span className="text-destructive">{analysisError}</span>
          ) : hasConnectedDb ? (
            "Click Analyze Schema to fetch the semantic layer and metadata from the backend."
          ) : (
            <div className="rounded-2xl border border-dashed border-border bg-background/70 p-5 text-left shadow-[var(--shadow-soft)]">
              <div className="text-[10px] uppercase tracking-[0.28em] text-muted-foreground">Ready when you are</div>
              <h3 className="mt-1 text-sm font-semibold text-foreground">Connect a database and click “Analyze Schema” to begin</h3>
              <p className="mt-2 text-xs text-muted-foreground">The semantic layer and grouped intelligence will appear here once your schema is analyzed.</p>
              <Button size="sm" className="mt-4 brand-gradient text-primary-foreground" onClick={onConnect}>Connect a database</Button>
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

function Metric({ icon: Icon, label, value }: { icon: any; label: string; value: string }) {
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

// ---------- Connect DB modal ----------
function ConnectDatabaseModal({
  open, onOpenChange, onAdd,
}: { open: boolean; onOpenChange: (b: boolean) => void; onAdd: (db: DB) => void }) {
  const [name, setName] = useState("");
  const [engine, setEngine] = useState("PostgreSQL");
  const [host, setHost] = useState("127.0.0.1");
  const [user, setUser] = useState("root");
  const [password, setPassword] = useState("");
  const [database, setDatabase] = useState("testdb");

  function submit() {
    if (!name.trim()) return;
    onAdd({ id: crypto.randomUUID(), name, engine, status: "connected", host, user, password, database });
    onOpenChange(false);
    setName(""); setHost("localhost"); setUser("root"); setPassword(""); setDatabase("testdb");
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="bg-card">
        <DialogHeader>
          <DialogTitle>Connect a database</DialogTitle>
          <DialogDescription>
            Add a new database connection. Credentials are encrypted and never leave your workspace.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div>
            <label className="mb-1 block text-xs text-muted-foreground">Connection name</label>
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="my_database" />
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted-foreground">Engine</label>
            <Select value={engine} onValueChange={setEngine}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                {["PostgreSQL", "MySQL", "BigQuery", "Snowflake", "ClickHouse", "Redshift"].map((e) => (
                  <SelectItem key={e} value={e}>{e}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted-foreground">Host</label>
            <Input value={host} onChange={(e) => setHost(e.target.value)} placeholder="localhost" />
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted-foreground">User</label>
            <Input value={user} onChange={(e) => setUser(e.target.value)} placeholder="root" />
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted-foreground">Password</label>
            <Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="••••••••" />
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted-foreground">Database</label>
            <Input value={database} onChange={(e) => setDatabase(e.target.value)} placeholder="testdb" />
          </div>
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button className="brand-gradient text-primary-foreground" onClick={submit}>Connect</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

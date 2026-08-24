// Client/Client-role Reports viewer (Milestone 3). A read-only consumer surface:
// lists reports published to the user's org and renders each by re-querying live
// (server-side, against the owner's connection). No SQL editor, no connections,
// no query box — and the client never receives SQL or credentials. On failure a
// report shows "needs attention" rather than stale data.
import { useCallback, useEffect, useState } from "react";
import { AlertCircle, BarChart3, LayoutDashboard, Loader2, LogOut, RefreshCw } from "lucide-react";

import { ChartRenderer, type ChartConfig, type ChartType } from "@/components/ChartRenderer";
import { DashboardsPanel } from "@/components/DashboardsPanel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ApiError } from "@/lib/api/client";
import { reportsApi, type ApiReport, type ReportRun } from "@/lib/api/platform";
import { useAuth } from "@/lib/auth";

export function ClientReports() {
  const { user, logout } = useAuth();
  const [reports, setReports] = useState<ApiReport[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [active, setActive] = useState<ApiReport | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setReports(await reportsApi.list());
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not load reports.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <div className="flex min-h-screen w-full flex-col bg-background text-foreground">
      <header className="flex items-center justify-between border-b border-border px-6 py-3 animate-fade-down">
        <div className="flex items-center gap-3">
          <div className="grid h-9 w-9 place-items-center rounded-xl brand-gradient transition-transform duration-200 hover:scale-105">
            <BarChart3 className="h-5 w-5 text-primary-foreground" />
          </div>
          <div>
            <h1 className="font-display text-lg font-semibold tracking-tight">Reports</h1>
            <p className="text-xs text-muted-foreground">{user?.full_name || user?.email}</p>
          </div>
        </div>
        <Button variant="outline" size="sm" className="gap-2" onClick={() => void logout()}>
          <LogOut className="h-4 w-4" /> Sign out
        </Button>
      </header>

      <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col px-6 py-6">
        <Tabs defaultValue="reports" className="flex min-h-0 flex-1 flex-col">
          <TabsList className="mb-4 self-start">
            <TabsTrigger value="reports" className="gap-1.5 text-xs">
              <BarChart3 className="h-3.5 w-3.5" /> Reports
            </TabsTrigger>
            <TabsTrigger value="dashboards" className="gap-1.5 text-xs">
              <LayoutDashboard className="h-3.5 w-3.5" /> Dashboards
            </TabsTrigger>
          </TabsList>

          <TabsContent value="dashboards" className="mt-0 flex min-h-0 flex-1 flex-col">
            {/* Read-only: a client can open and refresh a dashboard, never edit one. */}
            <DashboardsPanel canAuthor={false} canPublish={false} />
          </TabsContent>

          <TabsContent value="reports" className="mt-0 flex min-h-0 flex-1 flex-col">
            {loading ? (
              <div className="grid place-items-center py-24 text-muted-foreground">
                <Loader2 className="h-6 w-6 animate-spin" />
              </div>
            ) : error ? (
              <div className="grid place-items-center gap-2 py-24 text-center text-muted-foreground">
                <AlertCircle className="h-6 w-6 text-destructive" />
                <p>{error}</p>
              </div>
            ) : reports.length === 0 ? (
              <div className="grid place-items-center gap-2 rounded-2xl border border-border bg-card/60 py-24 text-center animate-fade-in">
                <BarChart3 className="h-8 w-8 text-muted-foreground/50 animate-float" />
                <p className="text-sm text-muted-foreground">
                  No reports have been shared with you yet.
                </p>
              </div>
            ) : (
              <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                {reports.map((r, i) => (
                  <button
                    key={r.id}
                    onClick={() => setActive(r)}
                    style={{ animationDelay: `${i * 70}ms` }}
                    className="group rounded-2xl border border-border bg-card p-5 text-left animate-fade-up transition-all duration-300 hover:-translate-y-1 hover:border-primary/40 hover:shadow-[0_12px_36px_-8px_oklch(0.92_0.03_240/0.2)]"
                  >
                    <div className="flex items-center justify-between">
                      <BarChart3 className="h-5 w-5 text-primary transition-transform duration-200 group-hover:scale-110" />
                      <Badge variant="secondary" className="text-[10px] uppercase">
                        {r.chart_type}
                      </Badge>
                    </div>
                    <h3 className="mt-3 font-display font-semibold">{r.title}</h3>
                    {r.nl_query && (
                      <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">
                        {r.nl_query}
                      </p>
                    )}
                    <p className="mt-3 text-[11px] text-muted-foreground/70">
                      Published {new Date(r.published_at).toLocaleDateString()}
                    </p>
                  </button>
                ))}
              </div>
            )}
          </TabsContent>
        </Tabs>
      </main>

      <ReportViewer report={active} onClose={() => setActive(null)} />
    </div>
  );
}

function ReportViewer({ report, onClose }: { report: ApiReport | null; onClose: () => void }) {
  const [run, setRun] = useState<ReportRun | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async (id: string, fallbackChartType: string | undefined) => {
    setLoading(true);
    setRun(null);
    try {
      setRun(await reportsApi.run(id));
    } catch (e) {
      setRun({
        ok: false,
        columns: [],
        rows: [],
        needs_attention: true,
        message: e instanceof ApiError ? e.message : "This report could not be loaded.",
        chart_type: fallbackChartType ?? "column",
        config: null,
      });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (report) void load(report.id, report.chart_type);
  }, [report, load]);

  return (
    <Dialog open={!!report} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            {report?.title}
            {report && (
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7"
                title="Refresh"
                onClick={() => report && void load(report.id, report.chart_type)}
              >
                <RefreshCw className="h-3.5 w-3.5" />
              </Button>
            )}
          </DialogTitle>
        </DialogHeader>

        {loading ? (
          <div className="grid place-items-center py-16 text-muted-foreground">
            <Loader2 className="h-6 w-6 animate-spin" />
          </div>
        ) : run && (!run.ok || run.needs_attention) ? (
          <div className="grid place-items-center gap-2 py-16 text-center">
            <AlertCircle className="h-7 w-7 text-amber-500" />
            <p className="text-sm font-medium">This report needs attention</p>
            <p className="max-w-sm text-xs text-muted-foreground">
              {run.message || "The data could not be loaded right now."}
            </p>
          </div>
        ) : run ? (
          <ReportRender chartType={report?.chart_type || "bar"} run={run} />
        ) : null}
      </DialogContent>
    </Dialog>
  );
}

function ReportRender({ chartType, run }: { chartType: string; run: ReportRun }) {
  // The run result carries the chart's latest saved type + colors, so a client
  // always sees the analyst's most recent customization. Fall back to the
  // report's metadata type, then a sensible default.
  const type = (run.chart_type || chartType || "column") as ChartType;
  return (
    <ChartRenderer
      columns={run.columns}
      rows={run.rows}
      type={type}
      config={(run.config as ChartConfig | null) ?? null}
      height={340}
    />
  );
}

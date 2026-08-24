// Dashboards (Infographics → Dashboards) — collections of pinned charts with
// narrative. A dashboard stores no data: opening one re-runs every chart live,
// concurrently, so a client always sees current numbers.
//
// Two views in one component:
//   • list  — the analyst's dashboards (or, for a client, the published ones)
//   • open  — a dashboard's charts, each with its description
//
// Each chart reports its own freshness and its own failure. A chart that cannot
// be read shows "needs attention" in place while its siblings render normally —
// one dead connection must not blank an eleven-chart dashboard.
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ArrowLeft,
  Check,
  ChevronDown,
  ChevronUp,
  LayoutDashboard,
  Loader2,
  Pencil,
  PinOff,
  Plus,
  RefreshCw,
  Sparkles,
  Trash2,
  TriangleAlert,
  Upload,
  X,
} from "lucide-react";
import { toast } from "sonner";

import { ChartRenderer, type ChartConfig, type ChartType } from "@/components/ChartRenderer";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { dashboardsApi, type ApiDashboard, type DashboardRun } from "@/lib/api/platform";

function relativeTime(iso: string | null): string {
  if (!iso) return "";
  const seconds = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  return `${Math.round(minutes / 60)}h ago`;
}

export type DashboardsPanelProps = {
  /** Analysts author; clients only open what was published to them. */
  canAuthor: boolean;
  canPublish: boolean;
};

export function DashboardsPanel({ canAuthor, canPublish }: DashboardsPanelProps) {
  const [dashboards, setDashboards] = useState<ApiDashboard[]>([]);
  const [loading, setLoading] = useState(true);
  const [openId, setOpenId] = useState<string | null>(null);
  const [newTitle, setNewTitle] = useState("");
  const [creating, setCreating] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const list = canAuthor ? await dashboardsApi.list() : await dashboardsApi.published();
      setDashboards(list);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Could not load dashboards.");
    } finally {
      setLoading(false);
    }
  }, [canAuthor]);

  useEffect(() => {
    void load();
  }, [load]);

  const create = useCallback(async () => {
    const title = newTitle.trim();
    if (!title) return;
    setCreating(true);
    try {
      const created = await dashboardsApi.create({ title });
      setDashboards((d) => [created, ...d]);
      setNewTitle("");
      toast.success(`Dashboard “${title}” created.`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Could not create the dashboard.");
    } finally {
      setCreating(false);
    }
  }, [newTitle]);

  const remove = useCallback(async (id: string) => {
    try {
      await dashboardsApi.remove(id);
      setDashboards((d) => d.filter((x) => x.id !== id));
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Could not delete the dashboard.");
    }
  }, []);

  if (openId) {
    return (
      <DashboardView
        dashboardId={openId}
        canAuthor={canAuthor}
        canPublish={canPublish}
        onBack={() => {
          setOpenId(null);
          void load();
        }}
      />
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
      {canAuthor && (
        <form
          className="mb-4 flex items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            void create();
          }}
        >
          <Input
            value={newTitle}
            onChange={(e) => setNewTitle(e.target.value)}
            placeholder="New dashboard name…"
            className="h-8 max-w-xs text-xs"
          />
          <Button
            type="submit"
            size="sm"
            variant="outline"
            className="h-8 gap-1.5 text-xs"
            disabled={creating || !newTitle.trim()}
          >
            {creating ? <Loader2 className="h-3 w-3 animate-spin" /> : <Plus className="h-3 w-3" />}
            Create
          </Button>
        </form>
      )}

      {loading ? (
        <div className="flex flex-1 items-center justify-center">
          <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
        </div>
      ) : dashboards.length === 0 ? (
        <div className="flex flex-1 items-center justify-center">
          <div className="text-center">
            <LayoutDashboard className="mx-auto h-12 w-12 text-muted-foreground/50" />
            <p className="mt-4 text-sm text-muted-foreground">No dashboards yet</p>
            <p className="mt-1 text-xs text-muted-foreground/70">
              {canAuthor
                ? "Create one above, then pin charts to it from the Charts tab."
                : "Published dashboards will appear here."}
            </p>
          </div>
        </div>
      ) : (
        <div className="space-y-2">
          {dashboards.map((d) => (
            <div
              key={d.id}
              className="flex items-center justify-between rounded-xl border border-border bg-card p-4"
            >
              <button
                type="button"
                className="min-w-0 flex-1 text-left"
                onClick={() => setOpenId(d.id)}
              >
                <div className="flex items-center gap-2">
                  <span className="truncate text-sm font-medium">{d.title}</span>
                  {d.status === "published" && (
                    <span className="rounded-full bg-primary/15 px-2 py-0.5 text-[10px] text-primary">
                      Published
                    </span>
                  )}
                </div>
                <p className="mt-0.5 text-xs text-muted-foreground">
                  {d.items.length} chart{d.items.length === 1 ? "" : "s"}
                </p>
              </button>
              {canAuthor && (
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-7 text-xs text-destructive hover:text-destructive"
                  onClick={() => void remove(d.id)}
                >
                  <Trash2 className="h-3 w-3" />
                </Button>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function DashboardView({
  dashboardId,
  canAuthor,
  canPublish,
  onBack,
}: {
  dashboardId: string;
  canAuthor: boolean;
  canPublish: boolean;
  onBack: () => void;
}) {
  const [meta, setMeta] = useState<ApiDashboard | null>(null);
  const [run, setRun] = useState<DashboardRun | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(
    async (refresh: boolean) => {
      if (refresh) setRefreshing(true);
      else setLoading(true);
      try {
        // Metadata and data are separate calls: the run is the expensive one, and
        // an analyst editing a description needs the item list back immediately
        // without paying for another full refresh.
        const [info, data] = await Promise.all([
          dashboardsApi.get(dashboardId),
          dashboardsApi.run(dashboardId, refresh),
        ]);
        setMeta(info);
        setRun(data);
      } catch (err) {
        toast.error(err instanceof Error ? err.message : "Could not open the dashboard.");
      } finally {
        setRefreshing(false);
        setLoading(false);
      }
    },
    [dashboardId],
  );

  useEffect(() => {
    void load(false);
  }, [load]);

  const publish = useCallback(
    async (next: boolean) => {
      try {
        const updated = next
          ? await dashboardsApi.publish(dashboardId)
          : await dashboardsApi.unpublish(dashboardId);
        setMeta(updated);
        toast.success(
          next ? "Dashboard published to your organization." : "Dashboard unpublished.",
        );
      } catch (err) {
        toast.error(err instanceof Error ? err.message : "Could not change publication.");
      }
    },
    [dashboardId],
  );

  const anyFailed = useMemo(() => (run?.charts ?? []).some((c) => !c.ok), [run]);

  // The oldest chart on the page governs what the header can honestly claim.
  const oldestFetch = useMemo(() => {
    const times = (run?.charts ?? [])
      .map((c) => c.fetched_at)
      .filter((t): t is string => Boolean(t));
    if (!times.length) return null;
    return times.reduce((a, b) => (new Date(a) < new Date(b) ? a : b));
  }, [run]);

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <Button size="sm" variant="ghost" className="h-8 gap-1.5 text-xs" onClick={onBack}>
            <ArrowLeft className="h-3 w-3" /> Back
          </Button>
          <div className="min-w-0">
            <h2 className="truncate text-sm font-semibold">{meta?.title ?? run?.title ?? "…"}</h2>
            {oldestFetch && (
              <p className="text-[10px] text-muted-foreground">
                Data as of {relativeTime(oldestFetch)}
              </p>
            )}
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          <Button
            size="sm"
            variant="outline"
            className="h-8 gap-1.5 text-xs"
            disabled={refreshing}
            onClick={() => void load(true)}
            title="Re-run every chart against the database, bypassing the cache"
          >
            {refreshing ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <RefreshCw className="h-3 w-3" />
            )}
            Refresh
          </Button>
          {canPublish && meta && (
            <Button
              size="sm"
              variant="outline"
              className="h-8 gap-1.5 text-xs"
              onClick={() => void publish(meta.status !== "published")}
            >
              <Upload className="h-3 w-3" />
              {meta.status === "published" ? "Unpublish" : "Publish"}
            </Button>
          )}
        </div>
      </div>

      {anyFailed && (
        <div className="mb-3 flex items-start gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 p-2.5 text-xs">
          <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>
            Some charts could not be refreshed and are marked below. The rest show current data.
          </span>
        </div>
      )}

      {loading ? (
        <div className="flex flex-1 items-center justify-center">
          <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
        </div>
      ) : (run?.charts.length ?? 0) === 0 ? (
        <div className="flex flex-1 items-center justify-center">
          <div className="text-center">
            <LayoutDashboard className="mx-auto h-12 w-12 text-muted-foreground/50" />
            <p className="mt-4 text-sm text-muted-foreground">This dashboard has no charts</p>
            <p className="mt-1 text-xs text-muted-foreground/70">
              Pin a chart from the Charts tab to get started.
            </p>
          </div>
        </div>
      ) : (
        <div className="space-y-4">
          {run!.charts.map((chart, index) => (
            <DashboardChartCard
              key={chart.item_id}
              dashboardId={dashboardId}
              chart={chart}
              canAuthor={canAuthor}
              canMoveUp={canAuthor && index > 0}
              canMoveDown={canAuthor && index < run!.charts.length - 1}
              onMove={async (direction) => {
                const ids = run!.charts.map((c) => c.item_id);
                const target = index + direction;
                [ids[index], ids[target]] = [ids[target], ids[index]];
                try {
                  await dashboardsApi.reorder(dashboardId, ids);
                  await load(false);
                } catch (err) {
                  toast.error(err instanceof Error ? err.message : "Could not reorder.");
                }
              }}
              onChanged={() => void load(false)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function DashboardChartCard({
  dashboardId,
  chart,
  canAuthor,
  canMoveUp,
  canMoveDown,
  onMove,
  onChanged,
}: {
  dashboardId: string;
  chart: DashboardRun["charts"][number];
  canAuthor: boolean;
  canMoveUp: boolean;
  canMoveDown: boolean;
  onMove: (direction: 1 | -1) => void | Promise<void>;
  onChanged: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(chart.description ?? "");
  const [saving, setSaving] = useState(false);
  const [drafting, setDrafting] = useState(false);
  // A description the AI proposed but nobody has accepted yet. Held separately
  // from `draft` so the analyst can see it as a suggestion and discard it.
  const [suggestion, setSuggestion] = useState<string | null>(null);

  const save = useCallback(
    async (text: string, fromAi: boolean) => {
      setSaving(true);
      try {
        if (fromAi) {
          await dashboardsApi.acceptDescription(dashboardId, chart.item_id, text);
        } else {
          await dashboardsApi.updateItem(dashboardId, chart.item_id, {
            description: text || null,
          });
        }
        setEditing(false);
        setSuggestion(null);
        onChanged();
      } catch (err) {
        toast.error(err instanceof Error ? err.message : "Could not save the description.");
      } finally {
        setSaving(false);
      }
    },
    [dashboardId, chart.item_id, onChanged],
  );

  const askAi = useCallback(async () => {
    setDrafting(true);
    try {
      const res = await dashboardsApi.describe(dashboardId, chart.item_id);
      setSuggestion(res.description);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Could not draft a description.");
    } finally {
      setDrafting(false);
    }
  }, [dashboardId, chart.item_id]);

  const unpin = useCallback(async () => {
    try {
      await dashboardsApi.unpin(dashboardId, chart.item_id);
      onChanged();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Could not unpin the chart.");
    }
  }, [dashboardId, chart.item_id, onChanged]);

  return (
    <div className="overflow-hidden rounded-xl border border-border bg-card">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-2.5">
        <div className="min-w-0">
          <h3 className="truncate text-sm font-medium">{chart.title}</h3>
          <p className="text-[10px] text-muted-foreground">
            {chart.ok
              ? // A truncated chart states its true total: plotting a slice must
                // never look like plotting the whole dataset.
                `${chart.truncated ? `${chart.rows.length.toLocaleString()} of ${chart.row_count.toLocaleString()}` : chart.rows.length.toLocaleString()} row${chart.row_count === 1 ? "" : "s"} · ${relativeTime(chart.fetched_at)}${chart.cached ? " · cached" : ""}`
              : "Not available"}
          </p>
        </div>
        {canAuthor && (
          <div className="flex items-center gap-1">
            <Button
              size="sm"
              variant="ghost"
              className="h-7 w-7 p-0"
              disabled={!canMoveUp}
              onClick={() => void onMove(-1)}
              title="Move up"
            >
              <ChevronUp className="h-3 w-3" />
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="h-7 w-7 p-0"
              disabled={!canMoveDown}
              onClick={() => void onMove(1)}
              title="Move down"
            >
              <ChevronDown className="h-3 w-3" />
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="h-7 w-7 p-0 text-destructive hover:text-destructive"
              onClick={() => void unpin()}
              title="Unpin from this dashboard"
            >
              <PinOff className="h-3 w-3" />
            </Button>
          </div>
        )}
      </div>

      {chart.ok ? (
        <div className="p-4">
          {chart.truncated && (
            <p className="mb-2 flex items-center gap-1.5 text-[10px] text-amber-500">
              <TriangleAlert className="h-3 w-3" />
              Showing the first {chart.rows.length.toLocaleString()} of{" "}
              {chart.row_count.toLocaleString()} rows. Aggregate this query for a complete picture.
            </p>
          )}
          <ChartRenderer
            columns={chart.columns}
            rows={chart.rows}
            type={chart.chart_type as ChartType}
            config={chart.config as ChartConfig | null}
            height={260}
          />
        </div>
      ) : (
        <div className="flex items-start gap-2 p-4 text-xs text-muted-foreground">
          <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-500" />
          <span>{chart.message ?? "This chart needs attention."}</span>
        </div>
      )}

      <div className="border-t border-border px-4 py-3">
        {editing ? (
          <div className="space-y-2">
            <Textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder="Describe what this chart shows…"
              className="min-h-20 text-xs"
            />
            {suggestion && (
              <div className="rounded-lg border border-primary/30 bg-primary/5 p-2.5">
                <p className="text-[10px] font-medium uppercase tracking-wide text-primary">
                  Suggested by AI — review before saving
                </p>
                <p className="mt-1 text-xs leading-relaxed">{suggestion}</p>
                <div className="mt-2 flex gap-1.5">
                  <Button
                    size="sm"
                    variant="outline"
                    className="h-7 gap-1.5 text-xs"
                    disabled={saving}
                    onClick={() => void save(suggestion, true)}
                  >
                    <Check className="h-3 w-3" /> Use this
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    className="h-7 text-xs"
                    onClick={() => setSuggestion(null)}
                  >
                    Discard
                  </Button>
                </div>
              </div>
            )}
            <div className="flex flex-wrap gap-1.5">
              <Button
                size="sm"
                variant="outline"
                className="h-7 gap-1.5 text-xs"
                disabled={saving}
                onClick={() => void save(draft, false)}
              >
                <Check className="h-3 w-3" /> Save
              </Button>
              <Button
                size="sm"
                variant="outline"
                className="h-7 gap-1.5 text-xs"
                disabled={drafting || !chart.ok}
                onClick={() => void askAi()}
                title={
                  chart.ok
                    ? "Draft a description from this chart's data"
                    : "The chart's data could not be read"
                }
              >
                {drafting ? (
                  <Loader2 className="h-3 w-3 animate-spin" />
                ) : (
                  <Sparkles className="h-3 w-3" />
                )}
                Ask AI
              </Button>
              <Button
                size="sm"
                variant="ghost"
                className="h-7 gap-1.5 text-xs"
                onClick={() => {
                  setEditing(false);
                  setSuggestion(null);
                  setDraft(chart.description ?? "");
                }}
              >
                <X className="h-3 w-3" /> Cancel
              </Button>
            </div>
          </div>
        ) : (
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              {chart.description ? (
                <p className="text-xs leading-relaxed text-muted-foreground">
                  {chart.description}
                  {chart.description_source === "ai" && (
                    <span className="ml-1.5 text-[10px] text-muted-foreground/70">
                      · AI-written
                    </span>
                  )}
                </p>
              ) : (
                <p className="text-xs italic text-muted-foreground/60">No description</p>
              )}
            </div>
            {canAuthor && (
              <Button
                size="sm"
                variant="ghost"
                className="h-7 shrink-0 gap-1.5 text-xs"
                onClick={() => setEditing(true)}
              >
                <Pencil className="h-3 w-3" />
                {chart.description ? "Edit" : "Add"}
              </Button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

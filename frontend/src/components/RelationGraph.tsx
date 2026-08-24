// Relation graph (Infographics → Relations) — analyst-only view of how databases
// and their tables interconnect. Two levels:
//   • overview: one node per database, edges inferred between databases;
//   • detail:   one node per table within a database, edges from foreign keys.
//
// Rendered in **real 3D** (WebGL, via react-force-graph-3d → three.js) with a
// force-directed layout the user can orbit, zoom and pan. Depth resolves the
// label-overlap problem a flat layout has at scale: nodes spread through three
// dimensions instead of a crowded plane, and labels are shown on hover rather
// than all at once. All graph *data* (including cross-DB inference) comes from
// the backend, which serves it from cached schema snapshots — the client only
// lays out and draws.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ForceGraph3D from "react-force-graph-3d";
import { Boxes, Database, Loader2, RefreshCw, TriangleAlert } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { relationsApi, type RelationDetail, type RelationOverview } from "@/lib/api/platform";

// ── Theme colours ───────────────────────────────────────────────────────────
//
// The WebGL scene needs concrete colours (three.js cannot parse a CSS custom
// property, and modern token palettes are authored in oklch(), which its colour
// parser also rejects). Resolve each token to the browser-computed rgb() string
// via a throwaway probe element, and re-resolve whenever the theme flips (the
// app stamps data-theme / a class on <html>), so the graph follows light/dark.

type ThemeColors = {
  chart1: string;
  chart2: string;
  chart3: string;
  accent: string;
  primary: string;
  muted: string;
  mutedFg: string;
  border: string;
  background: string;
  foreground: string;
};

function resolveColor(varName: string, fallback: string): string {
  if (typeof document === "undefined") return fallback;
  const probe = document.createElement("span");
  probe.style.color = `var(${varName})`;
  probe.style.display = "none";
  document.body.appendChild(probe);
  const raw = getComputedStyle(probe).color;
  probe.remove();
  if (!raw) return fallback;
  if (raw.startsWith("rgb")) return raw;
  // Modern token palettes are authored in oklch(), and current Chromium returns
  // the computed colour still in oklch — which three.js's colour parser rejects,
  // leaving every node/link the default black. Rasterise a single pixel to force
  // a plain sRGB rgb()/rgba() string that three.js understands, regardless of the
  // source colour space (oklch, lab, color(), …).
  try {
    const cvs = document.createElement("canvas");
    cvs.width = cvs.height = 1;
    const ctx = cvs.getContext("2d");
    if (!ctx) return raw;
    ctx.fillStyle = raw;
    ctx.fillRect(0, 0, 1, 1);
    const [r, g, b, a] = ctx.getImageData(0, 0, 1, 1).data;
    return a === 255 ? `rgb(${r}, ${g}, ${b})` : `rgba(${r}, ${g}, ${b}, ${(a / 255).toFixed(3)})`;
  } catch {
    return raw;
  }
}

function useThemeColors(): ThemeColors {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (typeof MutationObserver === "undefined") return;
    const obs = new MutationObserver(() => setTick((v) => v + 1));
    obs.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class", "data-theme", "style"],
    });
    return () => obs.disconnect();
  }, []);
  return useMemo(
    () => ({
      chart1: resolveColor("--chart-1", "#4f8ff7"),
      chart2: resolveColor("--chart-2", "#33c2a6"),
      chart3: resolveColor("--chart-3", "#e0a53f"),
      accent: resolveColor("--accent", "#8a8a8a"),
      primary: resolveColor("--primary", "#6ea8fe"),
      muted: resolveColor("--muted", "#555555"),
      mutedFg: resolveColor("--muted-foreground", "#9aa0aa"),
      border: resolveColor("--border", "#3a3a3a"),
      background: resolveColor("--background", "#0b0b0d"),
      foreground: resolveColor("--foreground", "#e6e6e6"),
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [tick],
  );
}

function engineColor(engine: string | null | undefined, c: ThemeColors): string {
  switch ((engine ?? "").toLowerCase()) {
    case "mysql":
      return c.chart1;
    case "postgresql":
      return c.chart2;
    case "sqlserver":
      return c.chart3;
    default:
      return c.accent;
  }
}

// ── 3D scene ──────────────────────────────────────────────────────────────────

// The graph objects react-force-graph mutates in place (it adds x/y/z, vx/vy/vz
// and resolves link source/target to node references), so callers must pass it
// throwaway clones — never the API state objects.
type GraphNode = Record<string, unknown> & { id: string };
type GraphLink = Record<string, unknown> & { source: string; target: string };

type Graph3DProps = {
  nodes: GraphNode[];
  links: GraphLink[];
  background: string;
  nodeColor: (n: GraphNode) => string;
  nodeVal: (n: GraphNode) => number;
  nodeLabel: (n: GraphNode) => string;
  linkColor: (l: GraphLink) => string;
  linkWidth: (l: GraphLink) => number;
  linkLabel: (l: GraphLink) => string;
  arrowLength: (l: GraphLink) => number;
  onNodeClick?: (n: GraphNode) => void;
  isClickable: (n: GraphNode) => boolean;
  legend: React.ReactNode;
};

function Graph3D(props: Graph3DProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const roRef = useRef<ResizeObserver | null>(null);
  // react-force-graph exposes imperative camera controls (zoomToFit) via the ref.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const fgRef = useRef<any>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });

  // ForceGraph needs explicit pixel dimensions, and the Relations tab has no size
  // until it is first shown — so mount the WebGL canvas only once the container is
  // measured. A callback ref measures synchronously the moment the node attaches
  // (the tab is already visible by then), which a post-mount effect + observer
  // missed here, leaving the canvas ungated at 0×0. A ResizeObserver then keeps it
  // in sync as the window/panel resizes.
  const setContainer = useCallback((el: HTMLDivElement | null) => {
    containerRef.current = el;
    roRef.current?.disconnect();
    roRef.current = null;
    if (!el) return;
    const measure = () => {
      const r = el.getBoundingClientRect();
      setSize({ w: Math.round(r.width), h: Math.round(r.height) });
    };
    measure();
    if (typeof ResizeObserver !== "undefined") {
      roRef.current = new ResizeObserver(measure);
      roRef.current.observe(el);
    }
  }, []);
  useEffect(() => () => roRef.current?.disconnect(), []);

  const graphData = useMemo(
    () => ({ nodes: props.nodes, links: props.links }),
    [props.nodes, props.links],
  );

  const fitView = useCallback(() => {
    fgRef.current?.zoomToFit(500, 60);
  }, []);

  const ready = size.w > 0 && size.h > 0;

  return (
    <div
      ref={setContainer}
      className="relative min-h-0 flex-1 overflow-hidden rounded-xl border border-border"
      style={{ background: props.background, cursor: "grab" }}
    >
      {ready && (
        <ForceGraph3D
          ref={fgRef}
          width={size.w}
          height={size.h}
          backgroundColor={props.background}
          graphData={graphData}
          nodeRelSize={4}
          nodeVal={props.nodeVal as never}
          nodeColor={props.nodeColor as never}
          nodeLabel={props.nodeLabel as never}
          nodeOpacity={0.92}
          linkColor={props.linkColor as never}
          linkWidth={props.linkWidth as never}
          linkOpacity={0.55}
          linkLabel={props.linkLabel as never}
          linkDirectionalArrowLength={props.arrowLength as never}
          linkDirectionalArrowRelPos={1}
          enableNodeDrag
          onNodeClick={((n: GraphNode) => props.onNodeClick?.(n)) as never}
          onNodeHover={
            ((n: GraphNode | null) => {
              const el = containerRef.current;
              if (el) el.style.cursor = n && props.isClickable(n) ? "pointer" : "grab";
            }) as never
          }
          // Frame the whole graph once the force sim settles, so it always opens
          // fully in view regardless of node count.
          onEngineStop={fitView as never}
        />
      )}
      <div className="pointer-events-none absolute inset-x-3 bottom-3 flex items-end justify-between gap-3">
        <div className="pointer-events-auto rounded-lg border border-border bg-card/90 px-3 py-1.5 backdrop-blur">
          {props.legend}
        </div>
        <Button
          size="sm"
          variant="outline"
          className="pointer-events-auto h-7 text-xs"
          onClick={fitView}
        >
          Reset view
        </Button>
      </div>
    </div>
  );
}

// "1,000 of 2,347 inferred links" when the backend capped the payload, plain
// "N inferred links" when it did not — so the count is never silently a floor.
function linkSummary(stats: RelationOverview["stats"]): string {
  const plural = stats.total_links === 1 ? "" : "s";
  if (!stats.truncated) {
    return `${stats.total_links.toLocaleString()} inferred link${plural}`;
  }
  return `${stats.links.toLocaleString()} of ${stats.total_links.toLocaleString()} inferred link${plural}`;
}

// ── Component ─────────────────────────────────────────────────────────────────

export function RelationGraph() {
  const [overview, setOverview] = useState<RelationOverview | null>(null);
  const [detail, setDetail] = useState<RelationDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadOverview = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setOverview(await relationsApi.overview());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load the relation graph.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadOverview();
  }, [loadOverview]);

  const openDetail = useCallback(async (connectionId: string, name: string) => {
    setLoading(true);
    setError(null);
    try {
      setDetail(await relationsApi.detail(connectionId));
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Failed to load tables.";
      // A DB with no snapshot yet returns 409 — guide the user to refresh it.
      toast.error(`Couldn't open ${name}`, { description: msg });
    } finally {
      setLoading(false);
    }
  }, []);

  const refreshAll = useCallback(async () => {
    setRefreshing(true);
    try {
      const res = await relationsApi.snapshotAll();
      const okCount = res.captured.length;
      if (res.failed.length) {
        toast.warning(`Snapshotted ${okCount}, ${res.failed.length} failed`, {
          description: res.failed.map((f) => f.name).join(", "),
        });
      } else if (okCount) {
        toast.success(`Snapshotted ${okCount} database${okCount === 1 ? "" : "s"}`);
      } else {
        toast.info("No databases to snapshot — connect one first.");
      }
      await loadOverview();
    } catch (e) {
      toast.error("Snapshot failed", { description: e instanceof Error ? e.message : "" });
    } finally {
      setRefreshing(false);
    }
  }, [loadOverview]);

  if (detail) {
    return <DetailView detail={detail} onBack={() => setDetail(null)} />;
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm text-muted-foreground">
            {overview
              ? `${overview.stats.databases} database${overview.stats.databases === 1 ? "" : "s"} · ${overview.stats.snapshotted} snapshotted · ${linkSummary(overview.stats)}`
              : "How your databases and tables interconnect."}
          </p>
          {overview?.stats.truncated && (
            // Truncation keeps the *top* of a confidence sort, so the links that
            // were dropped are entire weaker tiers — say so rather than let the
            // graph look complete.
            <p className="mt-0.5 flex items-center gap-1.5 text-xs text-amber-600 dark:text-amber-500">
              <TriangleAlert className="h-3.5 w-3.5 shrink-0" />
              Showing the {overview.stats.links.toLocaleString()} highest-confidence links only —
              weaker ones are not drawn.
            </p>
          )}
        </div>
        <Button
          size="sm"
          variant="outline"
          className="h-8 gap-1.5 text-xs"
          disabled={refreshing}
          onClick={() => void refreshAll()}
        >
          {refreshing ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <RefreshCw className="h-3.5 w-3.5" />
          )}
          Refresh snapshots
        </Button>
      </div>

      {loading && !overview ? (
        <GraphSkeleton />
      ) : error ? (
        <EmptyState icon={TriangleAlert} title="Couldn't load the graph" hint={error} />
      ) : !overview || overview.nodes.length === 0 ? (
        <EmptyState
          icon={Database}
          title="No databases connected"
          hint="Connect a database, then click “Refresh snapshots” to map how they relate."
        />
      ) : overview.stats.snapshotted === 0 ? (
        <EmptyState
          icon={Boxes}
          title="No schema snapshots yet"
          hint="Click “Refresh snapshots” to introspect your databases and build the graph."
        />
      ) : (
        <OverviewView overview={overview} onOpen={openDetail} />
      )}
    </div>
  );
}

// ── Overview (database-level) ─────────────────────────────────────────────────

function OverviewView({
  overview,
  onOpen,
}: {
  overview: RelationOverview;
  onOpen: (id: string, name: string) => void;
}) {
  const colors = useThemeColors();
  const maxTables = useMemo(
    () => Math.max(1, ...overview.nodes.map((n) => n.table_count)),
    [overview],
  );

  const nodes = useMemo<GraphNode[]>(() => overview.nodes.map((n) => ({ ...n })), [overview]);
  const links = useMemo<GraphLink[]>(
    () =>
      overview.edges.map((e) => ({
        source: e.source,
        target: e.target,
        confidence: e.confidence,
        _label: `${e.shared_count} shared: ${e.shared.join(", ")} · confidence ${(e.confidence * 100).toFixed(0)}%`,
      })),
    [overview],
  );

  return (
    <Graph3D
      nodes={nodes}
      links={links}
      background={colors.background}
      nodeColor={(n) =>
        (n as { has_snapshot?: boolean }).has_snapshot
          ? engineColor((n as { engine?: string }).engine, colors)
          : colors.muted
      }
      nodeVal={(n) =>
        2 + 12 * Math.sqrt(((n as { table_count?: number }).table_count ?? 0) / maxTables)
      }
      nodeLabel={(n) => {
        const d = n as unknown as {
          label: string;
          engine?: string;
          table_count?: number;
          has_snapshot?: boolean;
        };
        return d.has_snapshot
          ? `${d.label} · ${d.engine} · ${d.table_count} tables — click to explore`
          : `${d.label} · not snapshotted yet`;
      }}
      linkColor={() => colors.primary}
      linkWidth={(l) => 0.3 + ((l as { confidence?: number }).confidence ?? 0) * 1.8}
      linkLabel={(l) => (l as { _label?: string })._label ?? ""}
      arrowLength={() => 0}
      onNodeClick={(n) => {
        const d = n as { id: string; label: string; has_snapshot?: boolean };
        if (d.has_snapshot) onOpen(d.id, d.label);
      }}
      isClickable={(n) => !!(n as { has_snapshot?: boolean }).has_snapshot}
      legend={<OverviewLegend />}
    />
  );
}

function OverviewLegend() {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
      <span className="flex items-center gap-1.5">
        <span
          className="inline-block h-2.5 w-2.5 rounded-full"
          style={{ background: "var(--chart-1)" }}
        />{" "}
        MySQL
      </span>
      <span className="flex items-center gap-1.5">
        <span
          className="inline-block h-2.5 w-2.5 rounded-full"
          style={{ background: "var(--chart-2)" }}
        />{" "}
        PostgreSQL
      </span>
      <span className="flex items-center gap-1.5">
        <span
          className="inline-block h-2.5 w-2.5 rounded-full"
          style={{ background: "var(--chart-3)" }}
        />{" "}
        SQL Server
      </span>
      <span>· drag to rotate · scroll to zoom · thicker link = higher confidence · click a DB</span>
    </div>
  );
}

// ── Detail (table-level) ──────────────────────────────────────────────────────

function DetailView({ detail, onBack }: { detail: RelationDetail; onBack: () => void }) {
  const colors = useThemeColors();
  const maxCols = useMemo(() => Math.max(1, ...detail.nodes.map((n) => n.column_count)), [detail]);

  const nodes = useMemo<GraphNode[]>(() => detail.nodes.map((n) => ({ ...n })), [detail]);
  const links = useMemo<GraphLink[]>(
    () =>
      detail.edges.map((e) => ({
        source: e.source,
        target: e.target,
        kind: e.kind,
        _label: `${e.source}.${e.from_col} → ${e.target}.${e.to_col}${e.kind === "heuristic" ? " (inferred from naming)" : ""}`,
      })),
    [detail],
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <Button size="sm" variant="ghost" className="h-8 gap-1.5 text-xs" onClick={onBack}>
            ← All databases
          </Button>
          <p className="text-sm">
            <span className="font-medium">{detail.label}</span>
            <span className="text-muted-foreground">
              {" "}
              · {detail.stats.tables} tables · {detail.stats.relationships} relationships
            </span>
          </p>
        </div>
      </div>
      {detail.nodes.length === 0 ? (
        <EmptyState
          icon={Boxes}
          title="No tables in this snapshot"
          hint="Re-run the snapshot after the schema is populated."
        />
      ) : (
        <Graph3D
          nodes={nodes}
          links={links}
          background={colors.background}
          nodeColor={() => colors.chart2}
          nodeVal={(n) =>
            2 + 8 * Math.sqrt(((n as { column_count?: number }).column_count ?? 0) / maxCols)
          }
          nodeLabel={(n) => {
            const d = n as unknown as {
              label: string;
              column_count?: number;
              primary_keys?: string[];
            };
            const pk = d.primary_keys?.length ? ` · PK: ${d.primary_keys.join(", ")}` : "";
            return `${d.label} · ${d.column_count} columns${pk}`;
          }}
          linkColor={(l) =>
            (l as { kind?: string }).kind === "fk" ? colors.primary : colors.mutedFg
          }
          linkWidth={(l) => ((l as { kind?: string }).kind === "fk" ? 1.2 : 0.6)}
          linkLabel={(l) => (l as { _label?: string })._label ?? ""}
          arrowLength={(l) => ((l as { kind?: string }).kind === "fk" ? 3.5 : 0)}
          isClickable={() => false}
          legend={<DetailLegend />}
        />
      )}
    </div>
  );
}

function DetailLegend() {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
      <span className="flex items-center gap-1.5">
        <svg width="22" height="6">
          <line x1="0" y1="3" x2="22" y2="3" stroke="var(--primary)" strokeWidth="2" />
        </svg>
        declared foreign key
      </span>
      <span className="flex items-center gap-1.5">
        <svg width="22" height="6">
          <line
            x1="0"
            y1="3"
            x2="22"
            y2="3"
            stroke="var(--muted-foreground)"
            strokeWidth="2"
            strokeDasharray="5 3"
          />
        </svg>
        inferred from naming
      </span>
      <span>· drag to rotate · scroll to zoom · hover a table for columns</span>
    </div>
  );
}

// ── States ────────────────────────────────────────────────────────────────────

function GraphSkeleton() {
  return (
    <div className="flex min-h-0 flex-1 items-center justify-center rounded-xl border border-border">
      <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
    </div>
  );
}

function EmptyState({
  icon: Icon,
  title,
  hint,
}: {
  icon: typeof Database;
  title: string;
  hint: string;
}) {
  return (
    <div className="flex min-h-0 flex-1 items-center justify-center rounded-xl border border-dashed border-border">
      <div className="max-w-sm px-6 text-center">
        <Icon className="mx-auto h-10 w-10 text-muted-foreground/50" />
        <p className="mt-3 text-sm font-medium">{title}</p>
        <p className="mt-1 text-xs text-muted-foreground">{hint}</p>
      </div>
    </div>
  );
}

import {
  Activity,
  BarChart3,
  CheckCircle2,
  Code2,
  Database,
  Eye,
  FileText,
  LineChart,
  Lock,
  Share2,
  Sparkles,
} from "lucide-react";
import heroImage from "@/assets/hero-dashboard.jpg";

const pipelineRows = [
  { label: "Intent", value: "Revenue by region", state: "mapped" },
  { label: "Semantic layer", value: "orders.region + invoices.net", state: "verified" },
  { label: "Dialect", value: "PostgreSQL", state: "compiled" },
];

const reportRows = ["Executive revenue", "Client growth", "Weekly risk", "Board metrics"];

export function ProductPreview() {
  return (
    <div className="relative mx-auto max-w-xl lg:max-w-none">
      <div className="absolute -left-5 top-10 z-10 hidden w-40 rounded-lg border border-border bg-card/95 p-3 shadow-2xl backdrop-blur md:block motion-safe:animate-float">
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <Sparkles className="h-3.5 w-3.5 text-primary" /> Query plan
        </div>
        <div className="mt-2 text-2xl font-bold">98%</div>
        <div className="text-xs text-muted-foreground">schema confidence</div>
      </div>

      <div className="absolute -right-4 bottom-12 z-10 hidden w-44 rounded-lg border border-border bg-card/95 p-3 shadow-2xl backdrop-blur md:block motion-safe:animate-float [animation-delay:700ms]">
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <Share2 className="h-3.5 w-3.5 text-primary" /> Published
        </div>
        <div className="mt-3 space-y-2">
          {reportRows.slice(0, 3).map((row) => (
            <div key={row} className="flex items-center justify-between gap-2 text-xs">
              <span className="truncate text-muted-foreground">{row}</span>
              <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-primary" />
            </div>
          ))}
        </div>
      </div>

      <div className="overflow-hidden rounded-lg border border-border bg-card shadow-2xl transition-all duration-300 hover:border-primary/30 hover:shadow-[0_22px_70px_-22px_oklch(0.92_0.03_240/0.45)]">
        <div className="flex items-center justify-between border-b border-border bg-background/60 px-4 py-3">
          <div className="flex items-center gap-2">
            <span className="h-2.5 w-2.5 rounded-full bg-chart-5" />
            <span className="h-2.5 w-2.5 rounded-full bg-chart-4" />
            <span className="h-2.5 w-2.5 rounded-full bg-chart-3" />
          </div>
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <Lock className="h-3.5 w-3.5" /> RBAC active
          </div>
        </div>

        <div className="grid gap-0 lg:grid-cols-[0.95fr_1.05fr]">
          <div className="border-b border-border p-4 lg:border-b-0 lg:border-r">
            <div className="rounded-md border border-border bg-background/80 p-4">
              <div className="flex items-center gap-2 text-xs text-muted-foreground">
                <Database className="h-3.5 w-3.5 text-primary" /> Ask your warehouse
              </div>
              <p className="mt-3 text-sm leading-relaxed text-foreground">
                Show Q2 enterprise revenue by region, explain the joins, and publish the chart.
              </p>
            </div>

            <div className="mt-4 space-y-3">
              {pipelineRows.map((row) => (
                <div
                  key={row.label}
                  className="grid grid-cols-[88px_1fr_auto] items-center gap-3 text-xs"
                >
                  <span className="text-muted-foreground">{row.label}</span>
                  <span className="truncate font-medium">{row.value}</span>
                  <CheckCircle2 className="h-3.5 w-3.5 text-primary" aria-label={row.state} />
                </div>
              ))}
            </div>

            <div className="mt-5 rounded-md border border-border bg-background/70 p-3 font-mono text-[11px] leading-5 text-muted-foreground">
              <div className="text-primary">SELECT region, SUM(net_revenue)</div>
              <div>FROM invoices JOIN accounts</div>
              <div>WHERE quarter = 'Q2' GROUP BY region;</div>
            </div>
          </div>

          <div className="relative min-h-[320px] overflow-hidden bg-background/40">
            <img
              src={heroImage}
              alt="DB Buddy analytics dashboard showing charts, query results, and SQL context"
              className="h-full min-h-[320px] w-full object-cover opacity-80"
              width={1280}
              height={960}
            />
            <div className="absolute inset-x-0 bottom-0 border-t border-border bg-background/88 p-4 backdrop-blur">
              <div className="flex items-center justify-between gap-4">
                <div>
                  <div className="text-sm font-semibold">Client growth dashboard</div>
                  <div className="mt-1 text-xs text-muted-foreground">
                    Live chart, saved view, published report
                  </div>
                </div>
                <div className="flex items-center gap-2 text-xs text-primary">
                  <Activity className="h-3.5 w-3.5" /> Live
                </div>
              </div>
              <div className="mt-4 grid grid-cols-4 gap-2" aria-hidden="true">
                {[62, 84, 48, 76].map((height, index) => (
                  <span key={index} className="rounded-sm bg-primary/80" style={{ height }} />
                ))}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export function AnalyticsMockup() {
  return (
    <div className="overflow-hidden rounded-lg border border-border bg-card shadow-2xl">
      <img
        src={heroImage}
        alt="DB Buddy dashboard with interactive visual analytics"
        className="h-64 w-full object-cover opacity-85 sm:h-80"
        width={1280}
        height={960}
      />
      <div className="grid gap-px border-t border-border bg-border sm:grid-cols-3">
        {[
          { icon: BarChart3, label: "Charts", value: "12 saved" },
          { icon: LineChart, label: "Dashboards", value: "Live refresh" },
          { icon: FileText, label: "Reports", value: "Published" },
        ].map(({ icon: Icon, label, value }) => (
          <div key={label} className="bg-card p-4">
            <Icon className="h-4 w-4 text-primary" />
            <div className="mt-3 text-sm font-semibold">{label}</div>
            <div className="mt-1 text-xs text-muted-foreground">{value}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

export function AutomationMockup() {
  return (
    <div className="rounded-lg border border-border bg-card p-5 shadow-2xl">
      <div className="flex items-center justify-between gap-4 border-b border-border pb-4">
        <div>
          <div className="text-sm font-semibold">Automation queue</div>
          <div className="mt-1 text-xs text-muted-foreground">
            Reports, rebuilds, and notifications
          </div>
        </div>
        <span className="rounded-full border border-primary/30 bg-primary/10 px-3 py-1 text-xs text-primary">
          Running
        </span>
      </div>
      <div className="mt-5 space-y-4">
        {[
          { icon: FileText, title: "Weekly client report", meta: "Publishes Monday at 09:00" },
          {
            icon: Database,
            title: "Semantic context refresh",
            meta: "Keeps schema understanding current",
          },
          {
            icon: Eye,
            title: "Executive dashboard monitor",
            meta: "Notifies on threshold changes",
          },
          {
            icon: Code2,
            title: "Explainability snapshot",
            meta: "Stores SQL reasoning with output",
          },
        ].map(({ icon: Icon, title, meta }) => (
          <div key={title} className="grid grid-cols-[32px_1fr_auto] items-center gap-3">
            <div className="grid h-8 w-8 place-items-center rounded-md bg-primary/10 text-primary">
              <Icon className="h-4 w-4" />
            </div>
            <div>
              <div className="text-sm font-medium">{title}</div>
              <div className="mt-0.5 text-xs text-muted-foreground">{meta}</div>
            </div>
            <CheckCircle2 className="h-4 w-4 text-primary" />
          </div>
        ))}
      </div>
    </div>
  );
}

import { Sparkles, Settings2 } from "lucide-react";

export type SemanticColumn = {
  column: string;
  source?: string;
  provider?: string;
  plugin?: string;
};

export function SemanticGroup({ term, columns }: { term: string; columns: SemanticColumn[] }) {
  return (
    <section className="rounded-2xl border border-border bg-card/80 p-4 shadow-[var(--shadow-soft)]">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <p className="text-[10px] uppercase tracking-[0.28em] text-muted-foreground">Term</p>
          <h3 className="text-lg font-semibold tracking-tight text-foreground">{term}</h3>
        </div>
        <span className="rounded-full bg-primary/10 px-3 py-1 text-[11px] font-medium text-primary">{columns.length} columns</span>
      </div>

      <div className="space-y-2">
        {columns.map((col) => (
          <article key={col.column} className="rounded-xl border border-border/70 bg-background/70 p-3">
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="font-mono text-sm text-foreground">{col.column}</div>
                <div className="mt-1 flex flex-wrap gap-2 text-[11px] text-muted-foreground">
                  <span className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-2 py-0.5 text-primary">
                    <Sparkles className="h-3 w-3" /> {col.source ?? col.provider ?? "ai"}
                  </span>
                  {col.plugin ? (
                    <span className="inline-flex items-center gap-1 rounded-full bg-accent/10 px-2 py-0.5 text-foreground/80">
                      <Settings2 className="h-3 w-3" /> {col.plugin}
                    </span>
                  ) : null}
                </div>
              </div>
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}

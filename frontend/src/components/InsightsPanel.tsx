// Insights panel — evidence-bound explanations of an executed query result.
// Analyst-only; the API enforces that (schema:analyze), this component only
// avoids rendering a control the user cannot use.
//
// Deliberately not a chatbot UI. Every section is tied to the result set the
// user is looking at: findings carry their evidence inline, the answer to a
// follow-up is either grounded or an explicit "cannot determine", and nothing
// here can produce or run SQL.
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Check,
  Copy,
  Lightbulb,
  Loader2,
  RefreshCw,
  Send,
  Sparkles,
  TriangleAlert,
} from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  insightsApi,
  type InsightBundle,
  type InsightsSettings,
  type InsightsTurn,
} from "@/lib/api/platform";

// The exact sentence the backend returns when the evidence cannot support an
// answer. Matched so it can be styled as the honest refusal it is rather than
// read as a failure.
const CANNOT_DETERMINE = "I cannot determine that from the available data.";

// Findings below this confidence were tapered by the guardrail validator (the
// model hedged). They are shown, marked, and ranked last — not hidden.
const HEDGED_BELOW = 1;

export type InsightsPanelProps = {
  /** Column names, in display order. */
  columns: string[];
  /** Result rows as positional arrays, matching `columns`. */
  rows: (string | number)[][];
  sql: string;
  /** The natural-language question that produced this result. */
  question?: string;
  connectionId?: string | null;
  database?: string;
  chartType?: string | null;
};

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1.5">
      <h4 className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
        {title}
      </h4>
      {children}
    </div>
  );
}

export function InsightsPanel({
  columns,
  rows,
  sql,
  question,
  connectionId,
  database,
  chartType,
}: InsightsPanelProps) {
  const [settings, setSettings] = useState<InsightsSettings | null>(null);
  const [bundle, setBundle] = useState<InsightBundle | null>(null);
  const [suggested, setSuggested] = useState<string[]>([]);
  const [markdown, setMarkdown] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const [turns, setTurns] = useState<InsightsTurn[]>([]);
  const [followup, setFollowup] = useState("");
  const [asking, setAsking] = useState(false);

  // The API takes rows as objects; the chat view holds them positionally.
  const objectRows = useMemo(
    () =>
      rows.map((row) => {
        const obj: Record<string, unknown> = {};
        columns.forEach((c, i) => (obj[c] = row[i]));
        return obj;
      }),
    [columns, rows],
  );

  const body = useMemo(
    () => ({
      connection_id: connectionId ?? null,
      question: question ?? "",
      sql,
      database: database ?? "",
      rows: objectRows,
      chart_type: chartType ?? null,
    }),
    [connectionId, question, sql, database, objectRows, chartType],
  );

  useEffect(() => {
    let cancelled = false;
    insightsApi
      .settings()
      .then((s) => !cancelled && setSettings(s))
      .catch(() => !cancelled && setSettings(null));
    return () => {
      cancelled = true;
    };
  }, []);

  const generate = useCallback(
    async (regenerate: boolean) => {
      setLoading(true);
      setError(null);
      try {
        const res = await insightsApi.generate({ ...body, regenerate });
        setBundle(res.insights);
        setMarkdown(res.markdown);
        setSuggested(res.suggested_questions);
        // A regenerate starts the conversation over: follow-ups were answered
        // against the previous bundle, so keeping them would mix two analyses.
        if (regenerate) setTurns([]);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Could not generate insights.");
      } finally {
        setLoading(false);
      }
    },
    [body],
  );

  // Generation is explicit, never automatic: it costs a provider call, and an
  // analyst scrolling through results should not trigger one per result. A new
  // query clears the previous analysis and its conversation.
  useEffect(() => {
    setBundle(null);
    setTurns([]);
    setSuggested([]);
  }, [sql]);

  const ask = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || asking) return;
      setFollowup("");
      setTurns((t) => [...t, { role: "user", content: trimmed }]);
      setAsking(true);
      try {
        const res = await insightsApi.ask({ ...body, followup: trimmed, history: turns });
        setTurns((t) => [...t, { role: "assistant", content: res.answer }]);
      } catch (err) {
        setTurns((t) => [
          ...t,
          {
            role: "assistant",
            content: err instanceof Error ? err.message : "Could not answer that.",
          },
        ]);
      } finally {
        setAsking(false);
      }
    },
    [body, turns, asking],
  );

  const copy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(markdown);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      toast.error("Could not copy to the clipboard.");
    }
  }, [markdown]);

  if (settings && !settings.enabled) {
    return (
      <div className="p-4 text-xs text-muted-foreground">
        AI Insights is disabled for this deployment.
      </div>
    );
  }

  if (settings && !settings.configured) {
    return (
      <div className="flex items-start gap-2 p-4 text-xs text-muted-foreground">
        <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
        <span>
          No AI provider is configured for your organization. Add one in Settings → AI Providers to
          enable insights.
        </span>
      </div>
    );
  }

  return (
    <div className="space-y-4 p-4">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 text-xs font-medium">
          <Sparkles className="h-3.5 w-3.5 text-primary" />
          Insights
          {bundle?.cached && (
            <span className="text-[10px] font-normal text-muted-foreground">· cached</span>
          )}
          {bundle?.provider && (
            <span className="text-[10px] font-normal text-muted-foreground">
              · {bundle.provider}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1.5">
          {bundle && (
            <Button size="sm" variant="outline" className="h-7 gap-1.5 text-xs" onClick={copy}>
              {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
              {copied ? "Copied" : "Copy"}
            </Button>
          )}
          <Button
            size="sm"
            variant="outline"
            className="h-7 gap-1.5 text-xs"
            disabled={loading || rows.length === 0}
            onClick={() => generate(bundle !== null)}
            title={rows.length === 0 ? "There are no rows to analyze" : undefined}
          >
            {loading ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : bundle ? (
              <RefreshCw className="h-3 w-3" />
            ) : (
              <Lightbulb className="h-3 w-3" />
            )}
            {bundle ? "Regenerate" : "Analyze result"}
          </Button>
        </div>
      </div>

      {error && (
        <div className="flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/10 p-2.5 text-xs">
          <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {!bundle && !loading && !error && (
        <p className="text-xs text-muted-foreground">
          Explain these {rows.length} row{rows.length === 1 ? "" : "s"} using only what the result
          contains. No SQL is generated, and nothing outside this result is used.
        </p>
      )}

      {bundle && (
        <div className="space-y-4">
          <Section title="Executive summary">
            <p
              className={`text-xs leading-relaxed ${
                bundle.summary === CANNOT_DETERMINE ? "text-muted-foreground italic" : ""
              }`}
            >
              {bundle.summary}
            </p>
          </Section>

          {bundle.findings.length > 0 && (
            <Section title="Key findings">
              <ul className="space-y-2">
                {bundle.findings.map((f, i) => (
                  <li key={i} className="rounded-lg border border-border bg-background/50 p-2.5">
                    <div className="flex items-center gap-1.5">
                      <span className="text-xs font-medium">{f.title}</span>
                      {f.confidence < HEDGED_BELOW && (
                        <span
                          className="text-[9px] uppercase tracking-wide text-muted-foreground"
                          title="The model hedged on this one; the underlying numbers still stand."
                        >
                          qualified
                        </span>
                      )}
                    </div>
                    <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{f.detail}</p>
                    <p className="mt-1.5 border-l-2 border-border pl-2 text-[10px] text-muted-foreground">
                      Evidence: {f.evidence}
                    </p>
                  </li>
                ))}
              </ul>
            </Section>
          )}

          {bundle.recommendations.length > 0 && (
            <Section title="Recommendations">
              <ul className="list-disc space-y-1 pl-4 text-xs text-muted-foreground">
                {bundle.recommendations.map((r, i) => (
                  <li key={i}>{r}</li>
                ))}
              </ul>
            </Section>
          )}

          {bundle.limitations.length > 0 && (
            <Section title="Limitations">
              <ul className="list-disc space-y-1 pl-4 text-[10px] text-muted-foreground">
                {bundle.limitations.map((l, i) => (
                  <li key={i}>{l}</li>
                ))}
              </ul>
            </Section>
          )}

          <Section title="Ask a follow-up">
            <div className="space-y-2">
              {turns.length > 0 && (
                <div className="space-y-2">
                  {turns.map((t, i) => (
                    <div
                      key={i}
                      className={`rounded-lg p-2 text-xs leading-relaxed ${
                        t.role === "user"
                          ? "bg-primary/10"
                          : t.content === CANNOT_DETERMINE
                            ? "border border-border bg-background/50 italic text-muted-foreground"
                            : "border border-border bg-background/50"
                      }`}
                    >
                      {t.content}
                    </div>
                  ))}
                  {asking && (
                    <div className="flex items-center gap-1.5 text-[10px] text-muted-foreground">
                      <Loader2 className="h-3 w-3 animate-spin" /> Checking the result…
                    </div>
                  )}
                </div>
              )}

              {suggested.length > 0 && turns.length === 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {suggested.map((q) => (
                    <button
                      key={q}
                      type="button"
                      disabled={asking}
                      onClick={() => ask(q)}
                      className="rounded-full border border-border px-2 py-1 text-[10px] text-muted-foreground transition-colors hover:border-primary/50 hover:text-foreground disabled:opacity-50"
                    >
                      {q}
                    </button>
                  ))}
                </div>
              )}

              <form
                className="flex items-center gap-1.5"
                onSubmit={(e) => {
                  e.preventDefault();
                  ask(followup);
                }}
              >
                <Input
                  value={followup}
                  onChange={(e) => setFollowup(e.target.value)}
                  placeholder="Ask about this result…"
                  className="h-8 text-xs"
                  disabled={asking}
                />
                <Button
                  type="submit"
                  size="sm"
                  variant="outline"
                  className="h-8 w-8 p-0"
                  disabled={asking || !followup.trim()}
                >
                  <Send className="h-3 w-3" />
                </Button>
              </form>
            </div>
          </Section>
        </div>
      )}
    </div>
  );
}

import { createFileRoute, Link } from "@tanstack/react-router";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Sparkles,
  ArrowRight,
  Database,
  Shield,
  Zap,
  BarChart3,
  Code2,
  Users,
  Clock,
  CheckCircle2,
  Menu,
} from "lucide-react";
import heroImage from "@/assets/hero-dashboard.jpg";

export const Route = createFileRoute("/")({
  component: Landing,
  head: () => ({
    meta: [
      { title: "DB Buddy — Internal Data Intelligence Toolkit" },
      {
        name: "description",
        content:
          "DB Buddy is our internal AI assistant that turns plain-English questions into trusted SQL and insights across company databases.",
      },
    ],
  }),
});

function Landing() {
  return (
    <div className="min-h-screen bg-background text-foreground font-sans">
      {/* Nav */}
      <header className="sticky top-4 z-40 mx-auto max-w-6xl px-4 animate-fade-down">
        <nav className="flex items-center justify-between rounded-full border border-border/60 bg-card/80 px-4 py-2.5 backdrop-blur-xl shadow-sm transition-shadow duration-300 hover:shadow-[0_4px_24px_-6px_oklch(0.92_0.03_240/0.2)]">
          <Link to="/" className="flex items-center gap-2 group">
            <div className="grid h-8 w-8 place-items-center rounded-lg bg-primary text-primary-foreground transition-transform duration-200 group-hover:scale-110">
              <Database className="h-4 w-4" />
            </div>
            <span className="font-display font-bold tracking-tight">DB Buddy</span>
            <Badge variant="secondary" className="ml-1 hidden sm:inline-flex text-[10px] uppercase tracking-wider">
              Internal
            </Badge>
          </Link>
          <div className="hidden md:flex items-center gap-7 text-sm text-muted-foreground">
            {["Features", "How it works", "For teams", "Security"].map((label) => {
              const sectionId = label === "How it works" ? "how" : label === "For teams" ? "teams" : label.toLowerCase();
              return (
                <a
                  key={label}
                  href={`#${sectionId}`}
                  className="relative hover:text-foreground transition-colors duration-200 after:absolute after:bottom-[-2px] after:left-0 after:h-px after:w-0 after:bg-primary after:transition-all after:duration-300 hover:after:w-full"
                >
                  {label}
                </a>
              );
            })}
          </div>
          <div className="flex items-center gap-2">
            <Link to="/app">
              <Button size="sm" className="rounded-full">
                Open DB Buddy
              </Button>
            </Link>
            <Button size="icon" variant="secondary" className="rounded-full md:hidden">
              <Menu className="h-4 w-4" />
            </Button>
          </div>
        </nav>
      </header>

      {/* Hero */}
      <section className="relative overflow-hidden">
        {/* Grid texture */}
        <div
          className="pointer-events-none absolute inset-0 opacity-[0.06]"
          style={{
            backgroundImage:
              "linear-gradient(to right, hsl(var(--foreground)) 1px, transparent 1px), linear-gradient(to bottom, hsl(var(--foreground)) 1px, transparent 1px)",
            backgroundSize: "64px 64px",
            maskImage: "radial-gradient(ellipse at top, black 40%, transparent 75%)",
          }}
        />
        {/* Ambient glow orb */}
        <div
          className="pointer-events-none absolute -top-32 left-1/2 -translate-x-1/2 h-[500px] w-[800px] rounded-full opacity-[0.07]"
          style={{ background: "radial-gradient(ellipse, oklch(0.92 0.03 240) 0%, transparent 70%)" }}
        />

        <div className="relative mx-auto max-w-6xl px-6 pt-20 pb-24 grid lg:grid-cols-2 gap-12 items-center">
          <div>
            <div className="animate-fade-up">
              <Badge variant="secondary" className="rounded-full px-3 py-1 gap-1.5">
                <Sparkles className="h-3.5 w-3.5" /> Meet DB Buddy
              </Badge>
            </div>
            <h1 className="mt-6 font-display font-bold tracking-tight text-5xl md:text-6xl lg:text-7xl leading-[1.02] animate-fade-up delay-100">
              Ask your data.<br />
              Get answers your team can trust.
            </h1>
            <p className="mt-6 text-lg text-muted-foreground max-w-xl leading-relaxed animate-fade-up delay-200">
              DB Buddy is our internal toolkit for querying company databases in plain English.
              It writes the SQL, runs it against approved sources, and shows the result with the
              reasoning behind it — so analysts, PMs, and engineers can move at the same speed.
            </p>
            <div className="mt-8 flex flex-wrap items-center gap-3 animate-fade-up delay-300">
              <Link to="/app">
                <Button size="lg" className="rounded-full gap-2 animate-pulse-glow">
                  Launch DB Buddy <ArrowRight className="h-4 w-4" />
                </Button>
              </Link>
              <a href="#how">
                <Button size="lg" variant="secondary" className="rounded-full">
                  See how it works
                </Button>
              </a>
            </div>
          </div>

          {/* Hero card */}
          <div className="relative animate-fade-left delay-200">
            <div className="absolute -top-4 -left-4 z-10 rounded-2xl bg-card border border-border px-4 py-3 shadow-lg animate-fade-up delay-500 animate-float interactive-lift" style={{ animation: "fadeUp 0.55s cubic-bezier(0.22,1,0.36,1) 500ms both, float 6s ease-in-out 500ms infinite" }}>
              <div className="font-display font-bold text-2xl text-gradient-animate">AI</div>
              <div className="text-xs text-muted-foreground">SQL generation</div>
            </div>
            <div
              className="absolute -bottom-5 -left-2 z-10 rounded-2xl bg-card border border-border px-4 py-3 shadow-lg animate-float interactive-lift"
              style={{ animation: "fadeUp 0.55s cubic-bezier(0.22,1,0.36,1) 700ms both, float 6s ease-in-out 700ms infinite" }}
            >
              <div className="font-display font-bold text-2xl text-gradient-animate">Safe</div>
              <div className="text-xs text-muted-foreground">Execution flow</div>
            </div>
            <div
              className="absolute -right-3 top-1/3 z-10 rounded-2xl bg-card border border-border px-4 py-3 shadow-lg animate-float interactive-lift"
              style={{ animation: "fadeLeft 0.55s cubic-bezier(0.22,1,0.36,1) 600ms both, float 6s ease-in-out 600ms infinite" }}
            >
              <div className="font-display font-bold text-2xl text-gradient-animate">Smart</div>
              <div className="text-xs text-muted-foreground">Join routing</div>
            </div>
            <div className="overflow-hidden rounded-3xl border border-border shadow-2xl transition-all duration-500 hover:shadow-[0_24px_60px_-12px_oklch(0.92_0.03_240/0.3)] hover:border-primary/30 hover:scale-[1.01]">
              <img
                src={heroImage}
                alt="DB Buddy dashboard with analytics charts and SQL queries"
                width={1280}
                height={960}
                className="w-full h-auto object-cover"
              />
            </div>
          </div>
        </div>

        {/* Feature chips */}
        <div className="relative mx-auto max-w-6xl px-6 pb-16">
          <div className="flex flex-wrap justify-center gap-2">
            {[
              { icon: Code2, label: "AI-Powered SQL Generation" },
              { icon: Clock, label: "Real-time Insights" },
              { icon: Database, label: "Connects to Internal Warehouses" },
              { icon: Shield, label: "Role-aware & Audited" },
              { icon: BarChart3, label: "Inline Visualizations" },
              { icon: Users, label: "Built for Internal Teams" },
            ].map(({ icon: Icon, label }, idx) => (
              <div
                key={label}
                className="inline-flex items-center gap-2 rounded-full border border-border bg-card px-4 py-2 text-sm text-muted-foreground animate-scale-in cursor-default transition-all duration-200 hover:border-primary/40 hover:text-foreground hover:-translate-y-[1px]"
                style={{ animationDelay: `${idx * 80}ms` }}
              >
                <Icon className="h-3.5 w-3.5" /> {label}
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Features */}
      <section id="features" className="mx-auto max-w-6xl px-6 py-24">
        <div className="max-w-2xl">
          <Badge variant="secondary" className="rounded-full animate-fade-up">Why we built it</Badge>
          <h2 className="mt-4 font-display font-bold text-4xl md:text-5xl tracking-tight animate-fade-up delay-100">
            Schema-aware SQL generation that validates, explains, and safely executes.
          </h2>
          <p className="mt-4 text-muted-foreground text-lg animate-fade-up delay-200">
            Most NL-to-SQL tools translate questions blindly. DB Buddy first builds a semantic layer — a map of what every column in your database actually means — and grounds every SQL query in that understanding. It validates against your actual schema, provides join reasoning, and executes with safety controls.
          </p>
        </div>

        <div className="mt-12 grid md:grid-cols-2 lg:grid-cols-3 gap-4">
          {[
            {
              icon: Sparkles,
              title: "Semantic-layer grounded SQL",
              desc: "Every query is built against a structured understanding of your schema, not raw column names. Reduces hallucination and improves query correctness.",
            },
            {
              icon: Shield,
              title: "Safe execution with approval flow",
              desc: "SELECT queries run automatically; anything that mutates data waits for explicit sign-off. Dry-run previews show estimated impact before execution.",
            },
            {
              icon: Zap,
              title: "Self-healing query pipeline",
              desc: "Failed queries are repaired by AI and retried automatically, with confidence scoring on the result. Auto-fix loop with retry.",
            },
            {
              icon: Database,
              title: "Advanced join routing",
              desc: "BFS-based multi-hop join path finding with bidirectional graph traversal. Handles complex multi-table queries automatically.",
            },
            {
              icon: Code2,
              title: "FK-safe DELETE operations",
              desc: "Intelligent warnings showing dependent child tables before destructive operations. Prevents foreign key constraint failures.",
            },
            {
              icon: BarChart3,
              title: "Hybrid AI support",
              desc: "Runs fully offline via local models (Qwen/DeepSeek); falls back to Nemotron for complex queries. Local-first with cloud fallback.",
            },
          ].map(({ icon: Icon, title, desc }, idx) => (
            <div
              key={title}
              className="group rounded-2xl border border-border bg-card p-6 transition-all duration-300 hover:border-primary/30 hover:-translate-y-1 hover:shadow-[0_12px_36px_-8px_oklch(0.92_0.03_240/0.2)] animate-fade-up"
              style={{ animationDelay: `${idx * 80}ms` }}
            >
              <div className="grid h-10 w-10 place-items-center rounded-xl bg-primary text-primary-foreground transition-all duration-200 group-hover:scale-110 group-hover:shadow-[0_4px_16px_-2px_oklch(0.92_0.03_240/0.5)]">
                <Icon className="h-5 w-5" />
              </div>
              <h3 className="mt-5 font-display font-bold text-lg">{title}</h3>
              <p className="mt-2 text-sm text-muted-foreground leading-relaxed">{desc}</p>
            </div>
          ))}
        </div>
      </section>

      {/* How it works */}
      <section id="how" className="mx-auto max-w-6xl px-6 py-24 border-t border-border">
        <div className="grid lg:grid-cols-[1fr_1.2fr] gap-12 items-start">
          <div className="animate-fade-right">
            <Badge variant="secondary" className="rounded-full">How it works</Badge>
            <h2 className="mt-4 font-display font-bold text-4xl md:text-5xl tracking-tight">
              From question to answer in four steps.
            </h2>
            <p className="mt-4 text-muted-foreground text-lg">
              DB Buddy is designed for transparency. You always see what it's doing, why, and against
              which data source.
            </p>
            <Link to="/app">
              <Button size="lg" className="rounded-full mt-8 gap-2">
                Try it on a real question <ArrowRight className="h-4 w-4" />
              </Button>
            </Link>
          </div>
          <ol className="space-y-3">
            {[
              {
                step: "01",
                title: "Pick a database",
                desc: "Choose from the warehouses your role has access to. Your permissions follow you in.",
              },
              {
                step: "02",
                title: "Ask in plain English",
                desc: "\u201CHow many active accounts did EMEA add last quarter?\u201D \u2014 no SQL required.",
              },
              {
                step: "03",
                title: "Review the generated SQL",
                desc: "DB Buddy shows the query before running it. Edit, regenerate, or run as-is.",
              },
              {
                step: "04",
                title: "Get an explained result",
                desc: "Tables, charts, and a written interpretation. Save it for the team or export to a deck.",
              },
            ].map(({ step, title, desc }, idx) => (
              <li
                key={step}
                className="group rounded-2xl border border-border bg-card p-5 flex gap-5 transition-all duration-300 hover:border-primary/30 hover:-translate-x-1 hover:shadow-[0_4px_20px_-6px_oklch(0.92_0.03_240/0.2)] animate-fade-left"
                style={{ animationDelay: `${idx * 100}ms` }}
              >
                <div className="font-display font-bold text-2xl text-muted-foreground w-12 shrink-0 transition-colors duration-200 group-hover:text-primary/70">
                  {step}
                </div>
                <div>
                  <h3 className="font-display font-bold text-lg">{title}</h3>
                  <p className="mt-1 text-sm text-muted-foreground">{desc}</p>
                </div>
              </li>
            ))}
          </ol>
        </div>
      </section>

      {/* Teams */}
      <section id="teams" className="mx-auto max-w-6xl px-6 py-24 border-t border-border">
        <div className="max-w-2xl animate-fade-up">
          <Badge variant="secondary" className="rounded-full">For every team</Badge>
          <h2 className="mt-4 font-display font-bold text-4xl md:text-5xl tracking-tight">
            One toolkit, used very differently across the company.
          </h2>
        </div>
        <div className="mt-12 grid md:grid-cols-3 gap-4">
          {[
            {
              who: "Product & Growth",
              use: "Pull funnel and retention cuts without filing a ticket. Iterate on the question, not the SQL.",
            },
            {
              who: "Ops & Finance",
              use: "Reconcile numbers across systems and answer board questions before the meeting ends.",
            },
            {
              who: "Engineering & Data",
              use: "Skip the boilerplate. Use DB Buddy as a first draft, then promote good queries into dbt models.",
            },
          ].map((t, idx) => (
            <div
              key={t.who}
              className="rounded-2xl border border-border bg-card p-6 transition-all duration-300 hover:border-primary/30 hover:-translate-y-1 hover:shadow-[0_12px_36px_-8px_oklch(0.92_0.03_240/0.15)] animate-fade-up"
              style={{ animationDelay: `${idx * 100}ms` }}
            >
              <div className="text-sm text-muted-foreground">{t.who}</div>
              <p className="mt-3 text-base leading-relaxed">{t.use}</p>
            </div>
          ))}
        </div>
      </section>

      {/* Security */}
      <section id="security" className="mx-auto max-w-6xl px-6 py-24 border-t border-border">
        <div className="rounded-3xl border border-border bg-card p-10 md:p-14 grid lg:grid-cols-2 gap-10 items-center transition-all duration-500 hover:border-primary/20 hover:shadow-[0_20px_60px_-15px_oklch(0.92_0.03_240/0.15)] animate-scale-in">
          <div className="animate-fade-right">
            <Badge variant="secondary" className="rounded-full gap-1.5">
              <Shield className="h-3.5 w-3.5" /> Security &amp; governance
            </Badge>
            <h2 className="mt-4 font-display font-bold text-4xl tracking-tight">
              Safety-first execution with comprehensive validation.
            </h2>
            <p className="mt-4 text-muted-foreground">
              Every query is validated against your actual schema before execution. SELECT queries auto-execute, while INSERT/UPDATE/DELETE require explicit approval. The system includes silent failure detection for JOIN queries returning 0 rows.
            </p>
          </div>
          <ul className="space-y-3">
            {[
              "Query safety classification (READ vs WRITE detection)",
              "Dry-run previews with estimated row counts",
              "FK-safe DELETE with child table warnings",
              "Silent failure detection for zero-row joins",
              "Schema validation before execution",
              "Aggregation validation to prevent GROUP BY errors",
            ].map((p, idx) => (
              <li
                key={p}
                className="flex items-start gap-3 text-sm animate-fade-left"
                style={{ animationDelay: `${idx * 80}ms` }}
              >
                <CheckCircle2 className="h-5 w-5 text-primary mt-0.5 shrink-0 transition-transform duration-200 hover:scale-110" />
                <span>{p}</span>
              </li>
            ))}
          </ul>
        </div>
      </section>

      {/* CTA */}
      <section className="mx-auto max-w-6xl px-6 pb-24">
        <div className="rounded-3xl bg-primary text-primary-foreground p-12 md:p-16 text-center relative overflow-hidden animate-scale-in">
          {/* Shimmer sweep */}
          <div className="pointer-events-none absolute inset-0 animate-shimmer rounded-3xl" />
          <Zap className="h-8 w-8 mx-auto relative z-10 animate-float" />
          <h2 className="mt-4 font-display font-bold text-4xl md:text-5xl tracking-tight relative z-10">
            Your next question is waiting.
          </h2>
          <p className="mt-4 max-w-xl mx-auto opacity-80 relative z-10">
            Open DB Buddy with your company SSO and ask the question you would have filed a ticket for.
          </p>
          <Link to="/app" className="relative z-10 inline-block">
            <Button size="lg" variant="secondary" className="rounded-full mt-8 gap-2">
              Launch DB Buddy <ArrowRight className="h-4 w-4" />
            </Button>
          </Link>
        </div>
      </section>

      {/* Footer */}
      <footer className="border-t border-border">
        <div className="mx-auto max-w-6xl px-6 py-10 flex flex-col md:flex-row items-center justify-between gap-4 text-sm text-muted-foreground">
          <div className="flex items-center gap-2 group">
            <div className="grid h-7 w-7 place-items-center rounded-md bg-primary text-primary-foreground transition-transform duration-200 group-hover:scale-110">
              <Database className="h-3.5 w-3.5" />
            </div>
            <span className="font-display font-bold text-foreground">DB Buddy</span>
            <span>— Internal data intelligence toolkit</span>
          </div>
          <p className="text-xs max-w-md text-center md:text-right">
            DB Buddy can make mistakes. Always review generated SQL before using results in
            production decisions or external reporting.
          </p>
        </div>
      </footer>
    </div>
  );
}

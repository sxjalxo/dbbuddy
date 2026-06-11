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
      <header className="sticky top-4 z-40 mx-auto max-w-6xl px-4">
        <nav className="flex items-center justify-between rounded-full border border-border/60 bg-card/80 px-4 py-2.5 backdrop-blur-xl shadow-sm">
          <Link to="/" className="flex items-center gap-2">
            <div className="grid h-8 w-8 place-items-center rounded-lg bg-primary text-primary-foreground">
              <Database className="h-4 w-4" />
            </div>
            <span className="font-display font-bold tracking-tight">DB Buddy</span>
            <Badge variant="secondary" className="ml-1 hidden sm:inline-flex text-[10px] uppercase tracking-wider">
              Internal
            </Badge>
          </Link>
          <div className="hidden md:flex items-center gap-7 text-sm text-muted-foreground">
            <a href="#features" className="hover:text-foreground transition-colors">Features</a>
            <a href="#how" className="hover:text-foreground transition-colors">How it works</a>
            <a href="#teams" className="hover:text-foreground transition-colors">For teams</a>
            <a href="#security" className="hover:text-foreground transition-colors">Security</a>
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
        <div
          className="pointer-events-none absolute inset-0 opacity-[0.06]"
          style={{
            backgroundImage:
              "linear-gradient(to right, hsl(var(--foreground)) 1px, transparent 1px), linear-gradient(to bottom, hsl(var(--foreground)) 1px, transparent 1px)",
            backgroundSize: "64px 64px",
            maskImage: "radial-gradient(ellipse at top, black 40%, transparent 75%)",
          }}
        />
        <div className="relative mx-auto max-w-6xl px-6 pt-20 pb-24 grid lg:grid-cols-2 gap-12 items-center">
          <div>
            <Badge variant="secondary" className="rounded-full px-3 py-1 gap-1.5">
              <Sparkles className="h-3.5 w-3.5" /> Meet DB Buddy
            </Badge>
            <h1 className="mt-6 font-display font-bold tracking-tight text-5xl md:text-6xl lg:text-7xl leading-[1.02]">
              Ask your data.<br />
              Get answers your team can trust.
            </h1>
            <p className="mt-6 text-lg text-muted-foreground max-w-xl leading-relaxed">
              DB Buddy is our internal toolkit for querying company databases in plain English.
              It writes the SQL, runs it against approved sources, and shows the result with the
              reasoning behind it — so analysts, PMs, and engineers can move at the same speed.
            </p>
            <div className="mt-8 flex flex-wrap items-center gap-3">
              <Link to="/app">
                <Button size="lg" className="rounded-full gap-2">
                  Launch DB Buddy <ArrowRight className="h-4 w-4" />
                </Button>
              </Link>
              <a href="#how">
                <Button size="lg" variant="secondary" className="rounded-full">
                  See how it works
                </Button>
              </a>
            </div>
            <div className="mt-8 flex items-center gap-4 text-sm text-muted-foreground">
              <div className="flex -space-x-2">
                {["EN", "MR", "JK", "PL", "AS"].map((i) => (
                  <div
                    key={i}
                    className="h-9 w-9 rounded-full border-2 border-background bg-secondary grid place-items-center text-[11px] font-medium"
                  >
                    {i}
                  </div>
                ))}
              </div>
              <span>Used daily by 400+ teammates across Data, Ops, and Product</span>
            </div>
          </div>

          {/* Hero card */}
          <div className="relative">
            <div className="absolute -top-4 -left-4 z-10 rounded-2xl bg-card border border-border px-4 py-3 shadow-lg">
              <div className="font-display font-bold text-2xl">10×</div>
              <div className="text-xs text-muted-foreground">Faster ad-hoc queries</div>
            </div>
            <div className="absolute -bottom-5 -left-2 z-10 rounded-2xl bg-card border border-border px-4 py-3 shadow-lg">
              <div className="font-display font-bold text-2xl">80%</div>
              <div className="text-xs text-muted-foreground">Less analyst backlog</div>
            </div>
            <div className="absolute -right-3 top-1/3 z-10 rounded-2xl bg-card border border-border px-4 py-3 shadow-lg">
              <div className="font-display font-bold text-2xl">99%</div>
              <div className="text-xs text-muted-foreground">Schema-grounded</div>
            </div>
            <div className="overflow-hidden rounded-3xl border border-border shadow-2xl">
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
            ].map(({ icon: Icon, label }) => (
              <div
                key={label}
                className="inline-flex items-center gap-2 rounded-full border border-border bg-card px-4 py-2 text-sm text-muted-foreground"
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
          <Badge variant="secondary" className="rounded-full">Why we built it</Badge>
          <h2 className="mt-4 font-display font-bold text-4xl md:text-5xl tracking-tight">
            Less waiting on data. More deciding with it.
          </h2>
          <p className="mt-4 text-muted-foreground text-lg">
            Every team inside the company hits the same wall: questions pile up, the data team is
            booked out, dashboards go stale. DB Buddy puts a careful analyst between every teammate
            and our warehouses.
          </p>
        </div>

        <div className="mt-12 grid md:grid-cols-2 lg:grid-cols-3 gap-4">
          {[
            {
              icon: Sparkles,
              title: "Natural language → SQL",
              desc: "Ask in plain English. DB Buddy grounds answers in the actual schema, columns, and join paths it has access to.",
            },
            {
              icon: Code2,
              title: "Every query is reviewable",
              desc: "The generated SQL is shown alongside the result. Copy it, tweak it, save it to the team library, or open it in your editor.",
            },
            {
              icon: BarChart3,
              title: "Results that explain themselves",
              desc: "Tables, JSON, and charts inline — plus a short interpretation of what the numbers mean and which columns drove the answer.",
            },
            {
              icon: Shield,
              title: "Permissions stay yours",
              desc: "DB Buddy runs as you. Row-level rules, masking policies, and warehouse roles all apply — nothing is bypassed.",
            },
            {
              icon: Clock,
              title: "Shared history & saved queries",
              desc: "Recent questions and approved queries are visible to your team, so the answer to 'how did we calculate MRR?' lives in one place.",
            },
            {
              icon: Database,
              title: "Connects to what we already use",
              desc: "Postgres, BigQuery, Snowflake, and internal MySQL replicas. New sources are added by the platform team, not by every user.",
            },
          ].map(({ icon: Icon, title, desc }) => (
            <div
              key={title}
              className="rounded-2xl border border-border bg-card p-6 hover:border-foreground/20 transition-colors"
            >
              <div className="grid h-10 w-10 place-items-center rounded-xl bg-primary text-primary-foreground">
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
          <div>
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
                desc: "“How many active accounts did EMEA add last quarter?” — no SQL required.",
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
            ].map(({ step, title, desc }) => (
              <li key={step} className="rounded-2xl border border-border bg-card p-5 flex gap-5">
                <div className="font-display font-bold text-2xl text-muted-foreground w-12 shrink-0">
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
        <div className="max-w-2xl">
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
          ].map((t) => (
            <div key={t.who} className="rounded-2xl border border-border bg-card p-6">
              <div className="text-sm text-muted-foreground">{t.who}</div>
              <p className="mt-3 text-base leading-relaxed">{t.use}</p>
            </div>
          ))}
        </div>
      </section>

      {/* Security */}
      <section id="security" className="mx-auto max-w-6xl px-6 py-24 border-t border-border">
        <div className="rounded-3xl border border-border bg-card p-10 md:p-14 grid lg:grid-cols-2 gap-10 items-center">
          <div>
            <Badge variant="secondary" className="rounded-full gap-1.5">
              <Shield className="h-3.5 w-3.5" /> Security & governance
            </Badge>
            <h2 className="mt-4 font-display font-bold text-4xl tracking-tight">
              Built on the same controls our data team already trusts.
            </h2>
            <p className="mt-4 text-muted-foreground">
              DB Buddy never stores raw warehouse data, never bypasses warehouse roles, and logs every
              query for audit. It's deployed inside our network — not a third-party SaaS.
            </p>
          </div>
          <ul className="space-y-3">
            {[
              "SSO via the company identity provider",
              "All queries run with the caller's warehouse role",
              "Full query + result audit log retained for 90 days",
              "PII columns are masked according to data classification",
              "Self-hosted inside the corporate VPC — no external model exposure",
            ].map((p) => (
              <li key={p} className="flex items-start gap-3 text-sm">
                <CheckCircle2 className="h-5 w-5 text-primary mt-0.5 shrink-0" />
                <span>{p}</span>
              </li>
            ))}
          </ul>
        </div>
      </section>

      {/* CTA */}
      <section className="mx-auto max-w-6xl px-6 pb-24">
        <div className="rounded-3xl bg-primary text-primary-foreground p-12 md:p-16 text-center">
          <Zap className="h-8 w-8 mx-auto" />
          <h2 className="mt-4 font-display font-bold text-4xl md:text-5xl tracking-tight">
            Your next question is waiting.
          </h2>
          <p className="mt-4 max-w-xl mx-auto opacity-80">
            Open DB Buddy with your company SSO and ask the question you would have filed a ticket for.
          </p>
          <Link to="/app">
            <Button size="lg" variant="secondary" className="rounded-full mt-8 gap-2">
              Launch DB Buddy <ArrowRight className="h-4 w-4" />
            </Button>
          </Link>
        </div>
      </section>

      {/* Footer */}
      <footer className="border-t border-border">
        <div className="mx-auto max-w-6xl px-6 py-10 flex flex-col md:flex-row items-center justify-between gap-4 text-sm text-muted-foreground">
          <div className="flex items-center gap-2">
            <div className="grid h-7 w-7 place-items-center rounded-md bg-primary text-primary-foreground">
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

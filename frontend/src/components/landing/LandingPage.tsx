import { Link } from "@tanstack/react-router";
import { useEffect, type ReactNode } from "react";
import {
  ArrowRight,
  BarChart3,
  Bell,
  Boxes,
  Building2,
  CheckCircle2,
  Code2,
  Database,
  Eye,
  FileText,
  Gauge,
  Layers,
  Lightbulb,
  LineChart,
  Network,
  Search,
  Settings,
  Shield,
  ShieldCheck,
  Sparkles,
  Terminal,
  Zap,
  type LucideIcon,
} from "lucide-react";

import { Reveal } from "@/components/Reveal";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { AnalyticsMockup, AutomationMockup, ProductPreview } from "./ProductPreview";

type IconItem = {
  icon: LucideIcon;
  title: string;
  description: string;
};

const navItems = [
  { label: "Platform", href: "#platform" },
  { label: "Workflow", href: "#workflow" },
  { label: "Roles", href: "#roles" },
  { label: "Insights", href: "#insights" },
  { label: "Security", href: "#security" },
];

const platformCapabilities: IconItem[] = [
  {
    icon: Sparkles,
    title: "Natural-language analytics",
    description:
      "Business users ask questions directly while analysts keep visibility into the generated SQL.",
  },
  {
    icon: Code2,
    title: "Explainable SQL",
    description:
      "Every answer includes the query, join reasoning, confidence, and schema interpretation behind it.",
  },
  {
    icon: Lightbulb,
    title: "AI Insights",
    description:
      "Evidence-bound explanations of an executed result — findings and recommendations grounded in the data, never invented causes.",
  },
  {
    icon: BarChart3,
    title: "Interactive dashboards",
    description:
      "Turn trusted query output into live dashboards, charts, and saved visualizations.",
  },
  {
    icon: FileText,
    title: "Published reports",
    description:
      "Share read-only report views with the right audience without giving away database access.",
  },
  {
    icon: Network,
    title: "Data relationship mapping",
    description:
      "Visualize how databases and tables interconnect from stored schema snapshots — declared foreign keys plus inferred cross-database links.",
  },
  {
    icon: Shield,
    title: "Enterprise security",
    description:
      "Authentication, RBAC, MFA, audit logs, confirmed-write approvals, and protected credentials are built in.",
  },
  {
    icon: Bell,
    title: "Automation",
    description:
      "Schedule reports, refresh context, run background work, and notify teams when data changes.",
  },
  {
    icon: Building2,
    title: "Multi-tenant organizations",
    description:
      "Operate across organizations, users, roles, and client-facing experiences from one platform.",
  },
];

const coreCapabilities: IconItem[] = [
  {
    icon: Search,
    title: "Ask without losing control",
    description:
      "DB Buddy translates plain-English questions into planned analytics workflows instead of opaque guesses.",
  },
  {
    icon: Layers,
    title: "Semantic layer first",
    description:
      "Business terms are grounded in schema meaning, relationships, and dialect-aware constraints.",
  },
  {
    icon: Gauge,
    title: "Deterministic planning",
    description:
      "The query path is planned, validated, scored, and explained before the result becomes a chart or report. AI assists understanding; the system decides the SQL.",
  },
  {
    icon: Database,
    title: "One dialect layer, three engines",
    description:
      "MySQL, PostgreSQL, and SQL Server behind a single engine-agnostic pipeline — the same question compiles correctly on each.",
  },
  {
    icon: Boxes,
    title: "Bring your own AI",
    description:
      "Point labeling and insights at any OpenAI-compatible endpoint or a local Ollama server, with an automatic fallback chain and encrypted keys.",
  },
  {
    icon: Terminal,
    title: "CLI and API access",
    description:
      "Drive the same governed workflows from the terminal with personal API keys — query, customize charts, and publish without the web app.",
  },
];

const workflowSteps = [
  {
    step: "01",
    title: "Understand the question",
    description: "Intent, metrics, filters, and ambiguity are identified before SQL is drafted.",
  },
  {
    step: "02",
    title: "Map to governed context",
    description:
      "The semantic layer maps business language to tables, columns, relationships, and dialect capabilities.",
  },
  {
    step: "03",
    title: "Plan the query path",
    description:
      "DB Buddy chooses joins and aggregations through a deterministic, schema-aware planning pipeline.",
  },
  {
    step: "04",
    title: "Validate and explain",
    description:
      "Generated SQL is checked for safety, shape, and confidence, with reasoning exposed to the user.",
  },
  {
    step: "05",
    title: "Visualize or publish",
    description:
      "Results become charts, dashboards, saved views, scheduled reports, or read-only client experiences.",
  },
];

const roleCards = [
  {
    icon: Settings,
    role: "Platform Administrator",
    summary: "Run the whole platform: organizations, users, audit, and system & security settings.",
    points: [
      "Organization management",
      "Users and roles",
      "Audit dashboard",
      "System & security settings",
    ],
  },
  {
    icon: Building2,
    role: "Organization Admin",
    summary: "Own a single organization — its members, connections, AI settings, and audit trail.",
    points: ["Team member management", "Org AI providers", "Connections", "Scoped audit access"],
  },
  {
    icon: LineChart,
    role: "Analyst",
    summary: "Ask, inspect, visualize, save, publish, and automate analytics work.",
    points: ["Query workspace", "Dashboards & insights", "Publishing workflows", "Scheduled jobs"],
  },
  {
    icon: Eye,
    role: "Client",
    summary: "Consume trusted analytics through a focused, read-only report viewer.",
    points: ["Published reports", "Live dashboards", "Read-only access", "Clean client viewer"],
  },
];

const securityItems = [
  "Authentication and MFA",
  "Role-based access control",
  "Organization isolation",
  "Audit logs and activity history",
  "Encrypted connection credentials",
  "Parameterized, injection-safe queries",
  "Confirmed-write approval tokens",
  "Grounded, validated AI output",
];

const automationItems = [
  "Scheduled report delivery",
  "Background analytics jobs",
  "Semantic context rebuilds",
  "Dashboard refresh workflows",
  "Notifications for important updates",
  "Repeatable reporting operations",
];

export function LandingPage() {
  useEffect(() => {
    const previousMargin = document.body.style.margin;
    document.body.style.margin = "0";

    return () => {
      document.body.style.margin = previousMargin;
    };
  }, []);

  return (
    <div className="min-h-screen overflow-x-clip bg-background text-foreground font-sans">
      <Header />
      <main>
        <Hero />
        <PlatformOverview />
        <CoreCapabilities />
        <Workflow />
        <RoleExperiences />
        <VisualAnalytics />
        <AIInsights />
        <EnterpriseSecurity />
        <Automation />
        <FinalCta />
      </main>
      <Footer />
    </div>
  );
}

function Header() {
  return (
    <header className="sticky top-4 z-40 mx-auto max-w-6xl px-4 animate-fade-down">
      <nav className="flex items-center justify-between rounded-full border border-border/70 bg-card/85 px-4 py-2.5 shadow-sm backdrop-blur-xl">
        <Link
          to="/"
          className="flex items-center gap-2 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        >
          <span className="grid h-8 w-8 place-items-center rounded-md bg-primary text-primary-foreground">
            <Database className="h-4 w-4" />
          </span>
          <span className="font-display font-bold">DB Buddy</span>
        </Link>

        <div className="hidden items-center gap-7 text-sm text-muted-foreground md:flex">
          {navItems.map((item) => (
            <a
              key={item.href}
              href={item.href}
              className="transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            >
              {item.label}
            </a>
          ))}
        </div>

        <Button asChild size="sm" className="rounded-full">
          <Link to="/app">Open platform</Link>
        </Button>
      </nav>
    </header>
  );
}

function Hero() {
  return (
    <section className="relative overflow-hidden border-b border-border">
      <div
        className="pointer-events-none absolute inset-0 opacity-[0.055]"
        style={{
          backgroundImage:
            "linear-gradient(to right, hsl(var(--foreground)) 1px, transparent 1px), linear-gradient(to bottom, hsl(var(--foreground)) 1px, transparent 1px)",
          backgroundSize: "72px 72px",
          maskImage: "linear-gradient(to bottom, black 0%, black 62%, transparent 100%)",
        }}
      />
      <div className="relative mx-auto grid max-w-6xl gap-14 px-6 pb-20 pt-20 lg:grid-cols-[0.95fr_1.05fr] lg:items-center lg:pb-24 lg:pt-24">
        <div>
          <div className="animate-fade-up">
            <Badge variant="secondary" className="rounded-full px-3 py-1">
              Enterprise AI analytics platform
            </Badge>
          </div>
          <h1 className="mt-6 max-w-4xl font-display text-5xl font-bold leading-[1.02] md:text-6xl lg:text-7xl animate-fade-up delay-100">
            Trusted analytics from question to dashboard to report.
          </h1>
          <p className="mt-6 max-w-xl text-lg leading-8 text-muted-foreground animate-fade-up delay-200">
            DB Buddy gives teams an AI-assisted analytics platform for asking governed questions,
            inspecting explainable SQL, reading evidence-bound insights, building dashboards,
            publishing reports, and managing enterprise access across organizations.
          </p>
          <div className="mt-8 flex flex-wrap items-center gap-3 animate-fade-up delay-300">
            <Button asChild size="lg" className="rounded-full gap-2">
              <Link to="/app">
                Open DB Buddy <ArrowRight className="h-4 w-4" />
              </Link>
            </Button>
            <Button asChild size="lg" variant="secondary" className="rounded-full">
              <a href="#platform">Explore platform</a>
            </Button>
          </div>
          <div className="mt-9 grid max-w-xl grid-cols-1 gap-3 text-sm text-muted-foreground sm:grid-cols-2 lg:grid-cols-4 animate-fade-up delay-400">
            {[
              "MySQL, PostgreSQL & SQL Server",
              "Explainable SQL",
              "AI Insights",
              "RBAC + MFA + Audit",
            ].map((item) => (
              <div key={item} className="flex items-center gap-2">
                <CheckCircle2 className="h-4 w-4 shrink-0 text-primary" />
                <span>{item}</span>
              </div>
            ))}
          </div>
        </div>

        <div className="animate-fade-left delay-200">
          <ProductPreview />
        </div>
      </div>
    </section>
  );
}

function PlatformOverview() {
  return (
    <Section
      id="platform"
      eyebrow="Platform overview"
      title="More than natural-language SQL. A complete analytics operating layer."
      description="DB Buddy connects the path from question to governed answer: query planning, semantic context, charts, reports, access, auditability, and automation in one deployment-ready platform."
    >
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        {platformCapabilities.map((capability, index) => (
          <FeatureCard key={capability.title} item={capability} delay={index * 70} />
        ))}
      </div>
    </Section>
  );
}

function CoreCapabilities() {
  return (
    <section className="border-t border-border bg-card/25">
      <div className="mx-auto grid max-w-6xl gap-12 px-6 py-24 lg:grid-cols-[0.8fr_1.2fr] lg:items-start">
        <Reveal variant="right">
          <Badge variant="secondary" className="rounded-full">
            Core capabilities
          </Badge>
          <h2 className="mt-4 font-display text-4xl font-bold leading-tight md:text-5xl">
            Built for the full analytics lifecycle.
          </h2>
          <p className="mt-4 text-lg leading-8 text-muted-foreground">
            The product now covers the work before, during, and after a query: planning, validation,
            visualization, publishing, governance, and recurring delivery.
          </p>
        </Reveal>
        <div className="grid gap-4 sm:grid-cols-2">
          {coreCapabilities.map((capability, index) => (
            <FeatureCard key={capability.title} item={capability} delay={index * 90} />
          ))}
        </div>
      </div>
    </section>
  );
}

function Workflow() {
  return (
    <section id="workflow" className="border-t border-border">
      <div className="mx-auto grid max-w-6xl gap-12 px-6 py-24 lg:grid-cols-[0.9fr_1.1fr] lg:items-start">
        <Reveal variant="right">
          <Badge variant="secondary" className="rounded-full">
            How DB Buddy works
          </Badge>
          <h2 className="mt-4 font-display text-4xl font-bold leading-tight md:text-5xl">
            Deterministic planning, explainable results, publishable outcomes.
          </h2>
          <p className="mt-4 text-lg leading-8 text-muted-foreground">
            DB Buddy keeps AI useful by putting it inside a structured pipeline. The model helps
            interpret intent, while the platform validates decisions against schemas, roles,
            dialects, and safety controls.
          </p>
          <Button asChild size="lg" className="mt-8 rounded-full gap-2">
            <Link to="/app">
              Try a governed question <ArrowRight className="h-4 w-4" />
            </Link>
          </Button>
        </Reveal>

        <ol className="relative space-y-4 before:absolute before:left-6 before:top-6 before:h-[calc(100%-3rem)] before:w-px before:bg-border">
          {workflowSteps.map((step, index) => (
            <Reveal
              as="li"
              key={step.step}
              variant="left"
              delay={index * 90}
              className="relative grid grid-cols-[48px_1fr] gap-4"
            >
              <div className="relative z-10 grid h-12 w-12 place-items-center rounded-full border border-border bg-background font-display text-sm font-bold text-primary">
                {step.step}
              </div>
              <div className="rounded-lg border border-border bg-card p-5 transition-all duration-300 hover:border-primary/30 hover:-translate-y-1">
                <h3 className="font-display text-lg font-bold">{step.title}</h3>
                <p className="mt-2 text-sm leading-6 text-muted-foreground">{step.description}</p>
              </div>
            </Reveal>
          ))}
        </ol>
      </div>
    </section>
  );
}

function RoleExperiences() {
  return (
    <Section
      id="roles"
      eyebrow="Role-based experiences"
      title="Four roles, each with the right surface and nothing more."
      description="Platform admins, organization admins, analysts, and clients get distinct controls — so governance, authoring, and consumption stay separated without overexposing the system."
    >
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {roleCards.map(({ icon: Icon, role, summary, points }, index) => (
          <Reveal key={role} variant="up" delay={index * 100} className="h-full">
            <article className="group flex h-full flex-col rounded-lg border border-border bg-card p-6 transition-all duration-300 hover:border-primary/30 hover:-translate-y-1 hover:shadow-[0_16px_42px_-22px_oklch(0.92_0.03_240/0.45)]">
              <div className="grid h-11 w-11 place-items-center rounded-md bg-primary text-primary-foreground">
                <Icon className="h-5 w-5" />
              </div>
              <h3 className="mt-6 font-display text-2xl font-bold">{role}</h3>
              <p className="mt-3 text-sm leading-6 text-muted-foreground">{summary}</p>
              <ul className="mt-6 space-y-3">
                {points.map((point) => (
                  <li key={point} className="flex items-start gap-3 text-sm">
                    <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
                    <span>{point}</span>
                  </li>
                ))}
              </ul>
            </article>
          </Reveal>
        ))}
      </div>
    </Section>
  );
}

function VisualAnalytics() {
  return (
    <section id="visuals" className="border-t border-border bg-card/25">
      <div className="mx-auto grid max-w-6xl gap-12 px-6 py-24 lg:grid-cols-[1.1fr_0.9fr] lg:items-center">
        <Reveal variant="right">
          <AnalyticsMockup />
        </Reveal>
        <Reveal variant="left">
          <Badge variant="secondary" className="rounded-full">
            Visual analytics
          </Badge>
          <h2 className="mt-4 font-display text-4xl font-bold leading-tight md:text-5xl">
            From answer to artifact without leaving the platform.
          </h2>
          <p className="mt-4 text-lg leading-8 text-muted-foreground">
            Analysts can move from results to charts, dashboards, saved visualizations, live refresh
            views, and published reports without rebuilding context in another tool.
          </p>
          <FeatureList
            items={[
              "Interactive charts",
              "Dashboards and saved infographics",
              "Live refresh views",
              "Published report viewer",
            ]}
          />
        </Reveal>
      </div>
    </section>
  );
}

function AIInsights() {
  const insightItems = [
    "Findings grounded in the executed result",
    "Recommendations tied to real figures",
    "Follow-ups answered from the same evidence",
    "Invented causes removed, not hedged",
    "Honest provenance: AI or deterministic rule",
  ];
  return (
    <section id="insights" className="border-t border-border">
      <div className="mx-auto grid max-w-6xl gap-12 px-6 py-24 lg:grid-cols-[0.9fr_1.1fr] lg:items-center">
        <Reveal variant="right">
          <Badge variant="secondary" className="rounded-full">
            AI Insights
          </Badge>
          <h2 className="mt-4 font-display text-4xl font-bold leading-tight md:text-5xl">
            Explanations you can trust, because they can only cite the data.
          </h2>
          <p className="mt-4 text-lg leading-8 text-muted-foreground">
            The Insights Engine reads an executed result and returns summary, findings, and
            recommendations — never a chatbot guessing. Post-hoc validators enforce the boundary: a
            claim that names a cause outside the data is removed and disclosed, so a report can
            never narrate something the numbers do not contain.
          </p>
        </Reveal>

        <Reveal variant="left">
          <div className="grid gap-3 sm:grid-cols-2">
            {insightItems.map((item, index) => (
              <div
                key={item}
                className="flex items-start gap-3 rounded-lg border border-border bg-card p-4 transition-all duration-300 hover:border-primary/30"
                style={{ transitionDelay: `${index * 20}ms` }}
              >
                <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
                <span className="text-sm leading-6">{item}</span>
              </div>
            ))}
          </div>
        </Reveal>
      </div>
    </section>
  );
}

function EnterpriseSecurity() {
  return (
    <section id="security" className="border-t border-border">
      <div className="mx-auto grid max-w-6xl gap-12 px-6 py-24 lg:grid-cols-[0.9fr_1.1fr] lg:items-center">
        <Reveal variant="right">
          <Badge variant="secondary" className="rounded-full">
            Enterprise security
          </Badge>
          <h2 className="mt-4 font-display text-4xl font-bold leading-tight md:text-5xl">
            Governance that makes self-serve analytics acceptable.
          </h2>
          <p className="mt-4 text-lg leading-8 text-muted-foreground">
            DB Buddy is designed for teams that need speed and control at the same time: secure
            access, auditable actions, protected credentials, and clear review points for generated
            SQL.
          </p>
        </Reveal>

        <Reveal variant="left">
          <div className="grid gap-3 sm:grid-cols-2">
            {securityItems.map((item, index) => (
              <div
                key={item}
                className="flex items-start gap-3 rounded-lg border border-border bg-card p-4 transition-all duration-300 hover:border-primary/30"
                style={{ transitionDelay: `${index * 20}ms` }}
              >
                <Shield className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
                <span className="text-sm leading-6">{item}</span>
              </div>
            ))}
          </div>
        </Reveal>
      </div>
    </section>
  );
}

function Automation() {
  return (
    <section id="automation" className="border-t border-border bg-card/25">
      <div className="mx-auto grid max-w-6xl gap-12 px-6 py-24 lg:grid-cols-[0.9fr_1.1fr] lg:items-center">
        <Reveal variant="right">
          <Badge variant="secondary" className="rounded-full">
            Automation
          </Badge>
          <h2 className="mt-4 font-display text-4xl font-bold leading-tight md:text-5xl">
            Recurring analytics work, handled in the background.
          </h2>
          <p className="mt-4 text-lg leading-8 text-muted-foreground">
            Keep reporting and context current with scheduled delivery, background jobs, refresh
            workflows, and notifications that turn one-off answers into repeatable operations.
          </p>
          <FeatureList items={automationItems} columns />
        </Reveal>
        <Reveal variant="left">
          <AutomationMockup />
        </Reveal>
      </div>
    </section>
  );
}

function FinalCta() {
  return (
    <section className="border-t border-border">
      <div className="mx-auto max-w-6xl px-6 py-24">
        <Reveal
          variant="scale"
          className="relative overflow-hidden rounded-lg border border-primary/25 bg-primary p-10 text-center text-primary-foreground shadow-[0_22px_70px_-28px_oklch(0.92_0.03_240/0.75)] md:p-16"
        >
          <div className="pointer-events-none absolute inset-0 animate-shimmer motion-reduce:animate-none" />
          <div className="relative z-10 mx-auto grid h-12 w-12 place-items-center rounded-full bg-background text-foreground">
            <Zap className="h-5 w-5" />
          </div>
          <h2 className="relative z-10 mx-auto mt-6 max-w-3xl font-display text-4xl font-bold leading-tight md:text-5xl">
            Trusted analytics for enterprise teams.
          </h2>
          <p className="relative z-10 mx-auto mt-4 max-w-2xl text-base leading-7 opacity-80 md:text-lg">
            Ask governed questions, inspect the SQL, publish the answer, and keep every role inside
            the right boundary.
          </p>
          <div className="relative z-10 mt-8 flex flex-wrap justify-center gap-3">
            <Button asChild size="lg" variant="secondary" className="rounded-full gap-2">
              <Link to="/app">
                Open DB Buddy <ArrowRight className="h-4 w-4" />
              </Link>
            </Button>
            <Button
              asChild
              size="lg"
              className="rounded-full border border-background/20 bg-background/10 text-primary-foreground hover:bg-background/20"
            >
              <a href="#workflow">See workflow</a>
            </Button>
          </div>
        </Reveal>
      </div>
    </section>
  );
}

function Footer() {
  return (
    <footer className="border-t border-border">
      <Reveal
        variant="up"
        className="mx-auto flex max-w-6xl flex-col items-center justify-between gap-4 px-6 py-10 text-sm text-muted-foreground md:flex-row"
      >
        <div className="flex items-center gap-2">
          <span className="grid h-7 w-7 place-items-center rounded-md bg-primary text-primary-foreground">
            <Database className="h-3.5 w-3.5" />
          </span>
          <span className="font-display font-bold text-foreground">DB Buddy</span>
          <span>Enterprise AI analytics platform</span>
        </div>
        <p className="max-w-md text-center text-xs md:text-right">
          Review generated SQL and published outputs before relying on them for sensitive decisions.
        </p>
      </Reveal>
    </footer>
  );
}

function Section({
  id,
  eyebrow,
  title,
  description,
  children,
}: {
  id: string;
  eyebrow: string;
  title: string;
  description: string;
  children: ReactNode;
}) {
  return (
    <section id={id} className="border-t border-border">
      <div className="mx-auto max-w-6xl px-6 py-24">
        <Reveal className="mx-auto max-w-3xl text-center" variant="up">
          <Badge variant="secondary" className="rounded-full">
            {eyebrow}
          </Badge>
          <h2 className="mt-4 font-display text-4xl font-bold leading-tight md:text-5xl">
            {title}
          </h2>
          <p className="mt-4 text-lg leading-8 text-muted-foreground">{description}</p>
        </Reveal>
        <div className="mt-12">{children}</div>
      </div>
    </section>
  );
}

function FeatureCard({ item, delay = 0 }: { item: IconItem; delay?: number }) {
  const Icon = item.icon;
  return (
    <Reveal variant="up" delay={delay} className="h-full">
      <article className="group h-full rounded-lg border border-border bg-card p-6 transition-all duration-300 hover:border-primary/30 hover:-translate-y-1 hover:shadow-[0_16px_42px_-22px_oklch(0.92_0.03_240/0.45)]">
        <div className="grid h-10 w-10 place-items-center rounded-md bg-primary text-primary-foreground transition-transform duration-200 group-hover:scale-105">
          <Icon className="h-5 w-5" />
        </div>
        <h3 className="mt-5 font-display text-lg font-bold">{item.title}</h3>
        <p className="mt-2 text-sm leading-6 text-muted-foreground">{item.description}</p>
      </article>
    </Reveal>
  );
}

function FeatureList({ items, columns = false }: { items: string[]; columns?: boolean }) {
  return (
    <ul className={columns ? "mt-6 grid gap-3 sm:grid-cols-2" : "mt-6 space-y-3"}>
      {items.map((item) => (
        <li key={item} className="flex items-start gap-3 text-sm leading-6">
          <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
          <span>{item}</span>
        </li>
      ))}
    </ul>
  );
}

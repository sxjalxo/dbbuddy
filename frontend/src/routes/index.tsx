import { createFileRoute } from "@tanstack/react-router";

import { LandingPage } from "@/components/landing/LandingPage";

export const Route = createFileRoute("/")({
  component: LandingPage,
  head: () => ({
    meta: [
      { title: "DB Buddy - Enterprise AI Analytics Platform" },
      {
        name: "description",
        content:
          "DB Buddy is an enterprise AI analytics platform for governed natural-language querying, explainable SQL, evidence-bound AI insights, data relationship mapping, dashboards, published reports, multi-engine support (MySQL, PostgreSQL, SQL Server), role-based access, security, and automation.",
      },
    ],
  }),
});

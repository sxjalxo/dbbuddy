// Landing surface for the read-only User/Client role (no `query:run`, no
// `user:manage`). Analysts get the workspace and admins get the Admin console;
// this is the seam where the read-only Reports viewer (Milestone 3) will mount.
// Keeping it explicit means a User-role account never drops into the analyst UI
// where every action 403s.
import { BarChart3, Database, LogOut } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/lib/auth";

export function RoleLanding() {
  const { user, logout, hasPermission } = useAuth();
  const canViewReports = hasPermission("report:view");

  return (
    <div className="flex h-screen w-full items-center justify-center bg-background px-4 text-foreground">
      <div className="w-full max-w-md animate-scale-in">
        <div className="mb-6 flex flex-col items-center gap-2 text-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-2xl brand-gradient animate-fade-down animate-float">
            <BarChart3 className="h-6 w-6 text-primary-foreground" />
          </div>
          <h1 className="font-display text-2xl font-semibold tracking-tight">Reports</h1>
          <p className="text-sm text-muted-foreground">
            Signed in as {user?.full_name || user?.email}
          </p>
          <div className="flex flex-wrap justify-center gap-1.5">
            {(user?.roles ?? []).map((r, i) => (
              <Badge
                key={r}
                variant="secondary"
                className="text-[10px] uppercase tracking-wider animate-slide-in-chip"
                style={{ animationDelay: `${150 + i * 70}ms` }}
              >
                {r}
              </Badge>
            ))}
          </div>
        </div>

        <div className="rounded-2xl border border-border bg-card/80 p-6 text-center animate-fade-up delay-100 transition-colors duration-300 hover:border-primary/30">
          <Database className="mx-auto h-8 w-8 text-muted-foreground/60 animate-float" />
          <p className="mt-3 text-sm text-muted-foreground">
            {canViewReports
              ? "Published reports will appear here. An analyst hasn't shared any with you yet."
              : "Your account doesn't have access to any workspace yet. Ask an administrator to grant you a role."}
          </p>
        </div>

        <Button
          variant="outline"
          className="mt-4 w-full gap-2 animate-fade-up delay-200 interactive"
          onClick={() => {
            void logout();
          }}
        >
          <LogOut className="h-4 w-4" /> Sign out
        </Button>
      </div>
    </div>
  );
}

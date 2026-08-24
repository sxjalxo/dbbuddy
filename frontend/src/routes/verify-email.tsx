import { useEffect, useRef, useState } from "react";
import { createFileRoute, useNavigate, useSearch } from "@tanstack/react-router";
import { CheckCircle2, Database, Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { apiJson } from "@/lib/api/client";

/**
 * Where the emailed confirmation link lands: `/verify-email?token=…`.
 *
 * Redeemed on arrival rather than behind a button: the click already happened, in
 * the inbox. Asking the reader to click a second time to confirm that they meant
 * the first click is a step that only exists to make the page feel less empty.
 *
 * Guarded against React's development double-effect so one visit spends one
 * token — otherwise the second call redeems an already-used token and the page
 * reports failure for a confirmation that actually succeeded.
 */
function VerifyEmailPage() {
  const { token } = useSearch({ from: "/verify-email" });
  const navigate = useNavigate();
  const attempted = useRef(false);

  const [state, setState] = useState<"working" | "done" | "failed">(token ? "working" : "failed");
  const [error, setError] = useState<string | null>(
    token ? null : "This link is missing its token.",
  );

  useEffect(() => {
    if (!token || attempted.current) return;
    attempted.current = true;

    apiJson("/auth/verify-email/confirm", {
      method: "POST",
      body: JSON.stringify({ token }),
    })
      .then(() => setState("done"))
      .catch((err) => {
        setError(
          err instanceof Error ? err.message : "That confirmation link is invalid or has expired.",
        );
        setState("failed");
      });
  }, [token]);

  return (
    <div className="flex h-screen w-full items-center justify-center bg-background px-4 text-foreground">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex flex-col items-center gap-2 text-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-2xl brand-gradient">
            {state === "done" ? (
              <CheckCircle2 className="h-6 w-6 text-primary-foreground" />
            ) : (
              <Database className="h-6 w-6 text-primary-foreground" />
            )}
          </div>
          <h1 className="font-display text-2xl font-semibold tracking-tight">
            {state === "done" ? "Address confirmed" : "Confirming your address"}
          </h1>
          <p className="text-sm text-muted-foreground">
            {state === "working" && "One moment…"}
            {state === "done" && "You can sign in now."}
            {state === "failed" && "We couldn't confirm this link."}
          </p>
        </div>

        <div className="flex flex-col gap-3 rounded-2xl border border-border bg-card/80 p-6">
          {state === "working" && (
            <div className="flex items-center justify-center py-2">
              <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
            </div>
          )}

          {state === "failed" && (
            <p className="text-xs text-destructive">
              {error} Request a new one from the sign-in screen.
            </p>
          )}

          <Button
            variant={state === "done" ? "default" : "outline"}
            className={state === "done" ? "gap-2 brand-gradient text-primary-foreground" : ""}
            onClick={() => navigate({ to: "/app" })}
          >
            {state === "done" ? "Go to sign in" : "Back to sign in"}
          </Button>
        </div>
      </div>
    </div>
  );
}

export const Route = createFileRoute("/verify-email")({
  validateSearch: (search: Record<string, unknown>) => ({
    token: typeof search.token === "string" ? search.token : "",
  }),
  component: VerifyEmailPage,
  head: () => ({
    meta: [{ title: "Confirm your address - DB Buddy" }],
  }),
});

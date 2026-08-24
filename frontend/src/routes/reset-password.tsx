import { useState } from "react";
import { createFileRoute, useNavigate, useSearch } from "@tanstack/react-router";
import { Database, Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { apiJson } from "@/lib/api/client";

/**
 * Where the emailed reset link lands: `/reset-password?token=…`.
 *
 * A route of its own rather than a mode on the sign-in screen, because it is
 * reached by clicking a link in an inbox, not by navigating the app — the URL has
 * to mean something on a cold load, in whatever browser opened the mail.
 *
 * The token is never inspected here. The server decides whether it is unknown,
 * expired or already spent, and answers all three identically; guessing at that
 * in the client would either duplicate the rule or contradict it.
 */
function ResetPasswordPage() {
  const { token } = useSearch({ from: "/reset-password" });
  const navigate = useNavigate();

  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);

    // Checked here only because the server never sees the second field — it is a
    // typo guard, not a security control.
    if (password !== confirmation) {
      setError("Those passwords don't match.");
      return;
    }

    setBusy(true);
    try {
      await apiJson("/auth/password-reset/confirm", {
        method: "POST",
        body: JSON.stringify({ token, new_password: password }),
      });
      setDone(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "That reset link is invalid or has expired.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex h-screen w-full items-center justify-center bg-background px-4 text-foreground">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex flex-col items-center gap-2 text-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-2xl brand-gradient">
            <Database className="h-6 w-6 text-primary-foreground" />
          </div>
          <h1 className="font-display text-2xl font-semibold tracking-tight">
            Choose a new password
          </h1>
          <p className="text-sm text-muted-foreground">
            {done ? "You can sign in with it now." : "Signing in elsewhere will stop working."}
          </p>
        </div>

        {done ? (
          <div className="flex flex-col gap-3 rounded-2xl border border-border bg-card/80 p-6">
            <Button
              className="gap-2 brand-gradient text-primary-foreground"
              onClick={() => navigate({ to: "/app" })}
            >
              Go to sign in
            </Button>
          </div>
        ) : !token ? (
          <div className="flex flex-col gap-3 rounded-2xl border border-border bg-card/80 p-6">
            <p className="text-xs text-destructive">
              This link is missing its token. Request a new one from the sign-in screen.
            </p>
            <Button variant="outline" onClick={() => navigate({ to: "/app" })}>
              Back to sign in
            </Button>
          </div>
        ) : (
          <form
            onSubmit={submit}
            className="flex flex-col gap-3 rounded-2xl border border-border bg-card/80 p-6"
          >
            <div className="flex flex-col gap-1.5">
              <label className="text-xs font-medium text-muted-foreground">New password</label>
              <Input
                type="password"
                required
                minLength={8}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="At least 8 characters"
                autoComplete="new-password"
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <label className="text-xs font-medium text-muted-foreground">
                Confirm new password
              </label>
              <Input
                type="password"
                required
                minLength={8}
                value={confirmation}
                onChange={(e) => setConfirmation(e.target.value)}
                autoComplete="new-password"
              />
            </div>

            {error && <p className="text-xs text-destructive">{error}</p>}

            <Button
              type="submit"
              disabled={busy}
              className="mt-1 gap-2 brand-gradient text-primary-foreground"
            >
              {busy && <Loader2 className="h-4 w-4 animate-spin" />}
              Set new password
            </Button>
          </form>
        )}
      </div>
    </div>
  );
}

export const Route = createFileRoute("/reset-password")({
  validateSearch: (search: Record<string, unknown>) => ({
    token: typeof search.token === "string" ? search.token : "",
  }),
  component: ResetPasswordPage,
  head: () => ({
    meta: [{ title: "Reset your password - DB Buddy" }],
  }),
});

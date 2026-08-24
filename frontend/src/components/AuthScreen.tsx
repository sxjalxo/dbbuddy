import { useState } from "react";
import { Database, Loader2, ShieldCheck } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useAuth } from "@/lib/auth";

export function AuthScreen() {
  const { login, completeMfaLogin, register } = useAuth();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [fullName, setFullName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // When set, the password step succeeded and a TOTP/recovery code is required.
  const [challengeToken, setChallengeToken] = useState<string | null>(null);
  const [code, setCode] = useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      if (mode === "register") {
        await register(email.trim(), password, fullName.trim() || undefined);
      } else {
        const outcome = await login(email.trim(), password);
        if (outcome.mfaRequired) setChallengeToken(outcome.challengeToken);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong.");
    } finally {
      setBusy(false);
    }
  }

  async function submitCode(e: React.FormEvent) {
    e.preventDefault();
    if (!challengeToken) return;
    setError(null);
    setBusy(true);
    try {
      await completeMfaLogin(challengeToken, code.trim());
    } catch (err) {
      setError(err instanceof Error ? err.message : "That code didn't match.");
    } finally {
      setBusy(false);
    }
  }

  // ── Second-factor step ──────────────────────────────────────────────────────
  if (challengeToken) {
    return (
      <div className="flex h-screen w-full items-center justify-center bg-background px-4 text-foreground">
        <div className="w-full max-w-sm animate-scale-in">
          <div className="mb-6 flex flex-col items-center gap-2 text-center">
            <div className="flex h-12 w-12 items-center justify-center rounded-2xl brand-gradient animate-fade-down transition-transform duration-200 hover:scale-105">
              <ShieldCheck className="h-6 w-6 text-primary-foreground" />
            </div>
            <h1 className="font-display text-2xl font-semibold tracking-tight">
              Two-factor verification
            </h1>
            <p className="text-sm text-muted-foreground">
              Enter the 6-digit code from your authenticator app — or a recovery code.
            </p>
          </div>
          <form
            onSubmit={submitCode}
            className="flex flex-col gap-3 rounded-2xl border border-border bg-card/80 p-6"
          >
            <Input
              autoFocus
              value={code}
              onChange={(e) => setCode(e.target.value)}
              placeholder="123456"
              inputMode="text"
              autoComplete="one-time-code"
              className="text-center tracking-widest"
            />
            {error && <p className="text-xs text-destructive animate-fade-in">{error}</p>}
            <Button
              type="submit"
              disabled={busy || !code.trim()}
              className="mt-1 gap-2 brand-gradient text-primary-foreground"
            >
              {busy && <Loader2 className="h-4 w-4 animate-spin" />} Verify
            </Button>
            <button
              type="button"
              className="mt-1 text-center text-xs text-muted-foreground hover:text-foreground"
              onClick={() => {
                setChallengeToken(null);
                setCode("");
                setError(null);
              }}
            >
              Back to sign in
            </button>
          </form>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-screen w-full items-center justify-center bg-background px-4 text-foreground">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex flex-col items-center gap-2 text-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-2xl brand-gradient">
            <Database className="h-6 w-6 text-primary-foreground" />
          </div>
          <h1 className="font-display text-2xl font-semibold tracking-tight">DB Buddy</h1>
          <p className="text-sm text-muted-foreground">
            {mode === "login" ? "Sign in to your workspace" : "Create your account"}
          </p>
        </div>

        <form
          onSubmit={submit}
          className="flex flex-col gap-3 rounded-2xl border border-border bg-card/80 p-6"
        >
          {mode === "register" && (
            <div className="flex flex-col gap-1.5">
              <label className="text-xs font-medium text-muted-foreground">
                Full name (optional)
              </label>
              <Input
                value={fullName}
                onChange={(e) => setFullName(e.target.value)}
                placeholder="Ada Lovelace"
                autoComplete="name"
              />
            </div>
          )}
          <div className="flex flex-col gap-1.5">
            <label className="text-xs font-medium text-muted-foreground">Email</label>
            <Input
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com"
              autoComplete="email"
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <label className="text-xs font-medium text-muted-foreground">Password</label>
            <Input
              type="password"
              required
              minLength={8}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder={mode === "register" ? "At least 8 characters" : "••••••••"}
              autoComplete={mode === "login" ? "current-password" : "new-password"}
            />
          </div>

          {error && <p className="text-xs text-destructive">{error}</p>}

          <Button
            type="submit"
            disabled={busy}
            className="mt-1 gap-2 brand-gradient text-primary-foreground"
          >
            {busy && <Loader2 className="h-4 w-4 animate-spin" />}
            {mode === "login" ? "Sign in" : "Create account"}
          </Button>

          <button
            type="button"
            className="mt-1 text-center text-xs text-muted-foreground hover:text-foreground"
            onClick={() => {
              setError(null);
              setMode(mode === "login" ? "register" : "login");
            }}
          >
            {mode === "login" ? "No account? Create one" : "Already have an account? Sign in"}
          </button>
        </form>

        <p className="mt-4 text-center text-[11px] text-muted-foreground/70">
          Your workspace — connections, history, and charts — is tied to your account.
        </p>
      </div>
    </div>
  );
}

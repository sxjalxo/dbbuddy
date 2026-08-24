// Security settings — TOTP two-factor enrollment & removal (Milestone 5).
// Enroll: setup → scan QR → verify a code → store one-time recovery codes.
// Remove: re-authenticate with the current password.
import { useState } from "react";
import { Check, Copy, Loader2, ShieldCheck, ShieldOff } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError } from "@/lib/api/client";
import { mfaApi, type MfaSetup } from "@/lib/api/platform";
import { useAuth } from "@/lib/auth";

function msg(e: unknown, fallback: string) {
  return e instanceof ApiError || e instanceof Error ? e.message : fallback;
}

export function MfaCard() {
  const { user, refreshUser } = useAuth();
  const enabled = !!user?.mfa_enabled;

  const [setup, setSetup] = useState<MfaSetup | null>(null);
  const [code, setCode] = useState("");
  const [recovery, setRecovery] = useState<string[] | null>(null);
  const [disablePw, setDisablePw] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function begin() {
    setError(null);
    setBusy(true);
    try {
      setSetup(await mfaApi.setup());
    } catch (e) {
      setError(msg(e, "Could not start enrollment."));
    } finally {
      setBusy(false);
    }
  }

  async function verify() {
    setError(null);
    setBusy(true);
    try {
      const res = await mfaApi.verify(code.trim());
      setRecovery(res.recovery_codes);
      setSetup(null);
      setCode("");
      await refreshUser();
    } catch (e) {
      setError(msg(e, "That code didn't match."));
    } finally {
      setBusy(false);
    }
  }

  async function disable() {
    setError(null);
    setBusy(true);
    try {
      await mfaApi.disable({ password: disablePw });
      setDisablePw("");
      await refreshUser();
    } catch (e) {
      setError(msg(e, "Could not disable two-factor."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-xl border border-border bg-card/60 p-5">
      <div className="mb-3 flex items-center gap-2">
        {enabled ? (
          <ShieldCheck className="h-4 w-4 text-green-500" />
        ) : (
          <ShieldOff className="h-4 w-4 text-muted-foreground" />
        )}
        <h3 className="text-sm font-semibold">Two-factor authentication</h3>
      </div>

      {/* One-time recovery codes after enabling */}
      {recovery ? (
        <div className="space-y-3">
          <p className="text-xs text-muted-foreground">
            Save these recovery codes somewhere safe — each works once if you lose your
            authenticator. They won't be shown again.
          </p>
          <div className="grid grid-cols-2 gap-1.5 rounded-lg bg-background/60 p-3 font-mono text-xs">
            {recovery.map((c, i) => (
              <span
                key={c}
                className="animate-slide-in-chip"
                style={{ animationDelay: `${i * 35}ms` }}
              >
                {c}
              </span>
            ))}
          </div>
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="outline"
              className="gap-1.5"
              onClick={() => navigator.clipboard?.writeText(recovery.join("\n"))}
            >
              <Copy className="h-3.5 w-3.5" /> Copy
            </Button>
            <Button size="sm" className="gap-1.5" onClick={() => setRecovery(null)}>
              <Check className="h-3.5 w-3.5" /> I've saved them
            </Button>
          </div>
        </div>
      ) : enabled ? (
        // Enabled → offer disable (re-auth with password)
        <div className="space-y-2">
          <p className="text-xs text-muted-foreground">
            Two-factor is on. Sign-in requires a code from your authenticator.
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <Input
              type="password"
              value={disablePw}
              onChange={(e) => setDisablePw(e.target.value)}
              placeholder="Current password to disable"
              className="h-8 max-w-xs"
            />
            <Button
              size="sm"
              variant="outline"
              className="gap-1.5 text-destructive hover:text-destructive"
              disabled={busy || !disablePw}
              onClick={() => void disable()}
            >
              {busy ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <ShieldOff className="h-3.5 w-3.5" />
              )}
              Disable
            </Button>
          </div>
        </div>
      ) : setup ? (
        // Enrolling → show QR + verify
        <div className="space-y-3">
          <p className="text-xs text-muted-foreground">
            Scan this with Google Authenticator, 1Password, Authy, etc. — then enter the code it
            shows.
          </p>
          <div className="flex flex-col items-start gap-3 sm:flex-row">
            <div
              className="h-40 w-40 rounded-lg bg-white p-2 animate-scale-in [&_svg]:h-full [&_svg]:w-full"
              // Backend-generated SVG (our own server), safe to inline.
              dangerouslySetInnerHTML={{ __html: setup.qr_svg }}
            />
            <div className="flex-1 space-y-2">
              <p className="text-[11px] text-muted-foreground">Or enter this key manually:</p>
              <code className="block break-all rounded bg-background/60 px-2 py-1 font-mono text-[11px]">
                {setup.secret}
              </code>
              <Input
                value={code}
                onChange={(e) => setCode(e.target.value)}
                placeholder="6-digit code"
                inputMode="numeric"
                className="h-8 max-w-[160px] text-center tracking-widest"
              />
              <div className="flex gap-2">
                <Button
                  size="sm"
                  disabled={busy || code.trim().length < 6}
                  onClick={() => void verify()}
                >
                  {busy && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />} Verify & enable
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    setSetup(null);
                    setCode("");
                  }}
                >
                  Cancel
                </Button>
              </div>
            </div>
          </div>
        </div>
      ) : (
        // Not enabled → offer to start
        <div className="space-y-2">
          <p className="text-xs text-muted-foreground">
            Add a second factor (TOTP) for stronger account security.
          </p>
          <Button size="sm" className="gap-1.5" disabled={busy} onClick={() => void begin()}>
            {busy ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <ShieldCheck className="h-3.5 w-3.5" />
            )}
            Enable two-factor
          </Button>
        </div>
      )}

      {error && <p className="mt-2 text-xs text-destructive animate-fade-in">{error}</p>}
    </div>
  );
}

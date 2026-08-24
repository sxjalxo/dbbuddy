// Personal API keys (Settings → Personal API Keys). Long-lived credentials that
// let an analyst authenticate the CLI / automation with their own permissions.
// Create, rename, and revoke here — the same list the CLI's `dbbuddy keys`
// commands manage. The raw secret is shown exactly once, at creation.
import { useEffect, useState } from "react";
import { Check, Copy, KeyRound, Loader2, Pencil, Plus, Trash2, X } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { ApiError } from "@/lib/api/client";
import { apiKeysApi, type ApiKey, type ApiKeyCreated } from "@/lib/api/platform";

function msg(e: unknown, fallback: string) {
  return e instanceof ApiError || e instanceof Error ? e.message : fallback;
}

function fmtDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleDateString();
}

export function PersonalApiKeys() {
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  // The raw secret for a just-created key — shown once, then dismissed.
  const [revealed, setRevealed] = useState<ApiKeyCreated | null>(null);
  // Inline rename state, keyed by key id.
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editName, setEditName] = useState("");

  async function load() {
    try {
      setKeys(await apiKeysApi.list());
    } catch (e) {
      toast.error(msg(e, "Could not load API keys."));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  async function create() {
    const name = newName.trim();
    if (!name) return;
    setCreating(true);
    try {
      const created = await apiKeysApi.create(name);
      setRevealed(created);
      setNewName("");
      await load();
    } catch (e) {
      toast.error(msg(e, "Could not create API key."));
    } finally {
      setCreating(false);
    }
  }

  async function rename(id: string) {
    const name = editName.trim();
    if (!name) return;
    try {
      await apiKeysApi.rename(id, name);
      setEditingId(null);
      await load();
      toast.success("Key renamed.");
    } catch (e) {
      toast.error(msg(e, "Could not rename key."));
    }
  }

  async function revoke(id: string) {
    try {
      await apiKeysApi.revoke(id);
      await load();
      toast.success("Key revoked.");
    } catch (e) {
      toast.error(msg(e, "Could not revoke key."));
    }
  }

  function copy(value: string) {
    navigator.clipboard?.writeText(value);
    toast.success("Copied to clipboard.");
  }

  return (
    <div className="rounded-xl border border-border bg-card/60 p-5">
      <div className="mb-3 flex items-center gap-2">
        <KeyRound className="h-4 w-4 text-muted-foreground" />
        <h3 className="text-sm font-semibold">Personal API keys</h3>
      </div>
      <p className="mb-4 text-xs text-muted-foreground">
        Authenticate the DB Buddy CLI and automation with your own permissions. Use a key with{" "}
        <code className="rounded bg-background/60 px-1 py-0.5 font-mono text-[11px]">
          dbbuddy login --api-key
        </code>{" "}
        or the{" "}
        <code className="rounded bg-background/60 px-1 py-0.5 font-mono text-[11px]">
          DBBUDDY_API_KEY
        </code>{" "}
        environment variable.
      </p>

      {/* One-time reveal of a newly created key */}
      {revealed && (
        <div className="mb-4 space-y-2 rounded-lg border border-primary/40 bg-primary/5 p-3 animate-scale-in">
          <p className="text-xs font-medium">Copy your new key now — it won't be shown again.</p>
          <div className="flex items-center gap-2">
            <code className="flex-1 break-all rounded bg-background/60 px-2 py-1.5 font-mono text-[11px]">
              {revealed.api_key}
            </code>
            <Button
              size="sm"
              variant="outline"
              className="gap-1.5"
              onClick={() => copy(revealed.api_key)}
            >
              <Copy className="h-3.5 w-3.5" /> Copy
            </Button>
          </div>
          <Button size="sm" className="gap-1.5" onClick={() => setRevealed(null)}>
            <Check className="h-3.5 w-3.5" /> I've saved it
          </Button>
        </div>
      )}

      {/* Create */}
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <Input
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && void create()}
          placeholder="Key name (e.g. laptop, ci-pipeline)"
          className="h-8 max-w-xs"
        />
        <Button
          size="sm"
          className="gap-1.5"
          disabled={creating || !newName.trim()}
          onClick={() => void create()}
        >
          {creating ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <Plus className="h-3.5 w-3.5" />
          )}
          Create key
        </Button>
      </div>

      {/* List */}
      {loading ? (
        <div className="flex items-center gap-2 py-4 text-xs text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading keys…
        </div>
      ) : keys.length === 0 ? (
        <p className="py-3 text-xs text-muted-foreground">No API keys yet.</p>
      ) : (
        <ul className="divide-y divide-border/60">
          {keys.map((k) => {
            const revoked = !!k.revoked_at;
            return (
              <li
                key={k.id}
                className={`flex flex-wrap items-center justify-between gap-2 py-2.5 ${revoked ? "opacity-60" : ""}`}
              >
                <div className="min-w-0">
                  {editingId === k.id ? (
                    <div className="flex items-center gap-1.5">
                      <Input
                        value={editName}
                        onChange={(e) => setEditName(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") void rename(k.id);
                          if (e.key === "Escape") setEditingId(null);
                        }}
                        autoFocus
                        className="h-7 max-w-[200px] text-sm"
                      />
                      <Button
                        size="icon"
                        variant="ghost"
                        className="h-7 w-7"
                        onClick={() => void rename(k.id)}
                      >
                        <Check className="h-3.5 w-3.5" />
                      </Button>
                      <Button
                        size="icon"
                        variant="ghost"
                        className="h-7 w-7"
                        onClick={() => setEditingId(null)}
                      >
                        <X className="h-3.5 w-3.5" />
                      </Button>
                    </div>
                  ) : (
                    <div className="flex items-center gap-2">
                      <span className="truncate text-sm font-medium">{k.name}</span>
                      <code className="rounded bg-background/60 px-1 py-0.5 font-mono text-[10px] text-muted-foreground">
                        dbk_{k.token_prefix}…
                      </code>
                      {revoked ? (
                        <Badge variant="outline" className="text-[10px]">
                          Revoked
                        </Badge>
                      ) : (
                        <Badge variant="secondary" className="text-[10px]">
                          Active
                        </Badge>
                      )}
                    </div>
                  )}
                  <div className="mt-0.5 text-[11px] text-muted-foreground">
                    Created {fmtDate(k.created_at)} · Last used{" "}
                    {k.last_used_at ? fmtDate(k.last_used_at) : "never"}
                  </div>
                </div>

                {!revoked && editingId !== k.id && (
                  <div className="flex items-center gap-1">
                    <Button
                      size="icon"
                      variant="ghost"
                      className="h-7 w-7"
                      title="Rename"
                      onClick={() => {
                        setEditingId(k.id);
                        setEditName(k.name);
                      }}
                    >
                      <Pencil className="h-3.5 w-3.5" />
                    </Button>
                    <AlertDialog>
                      <AlertDialogTrigger asChild>
                        <Button
                          size="icon"
                          variant="ghost"
                          className="h-7 w-7 text-destructive hover:text-destructive"
                          title="Revoke"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      </AlertDialogTrigger>
                      <AlertDialogContent>
                        <AlertDialogHeader>
                          <AlertDialogTitle>Revoke “{k.name}”?</AlertDialogTitle>
                          <AlertDialogDescription>
                            This immediately stops the key from working everywhere — any CLI or
                            script using it will need a new key. This can't be undone.
                          </AlertDialogDescription>
                        </AlertDialogHeader>
                        <AlertDialogFooter>
                          <AlertDialogCancel>Cancel</AlertDialogCancel>
                          <AlertDialogAction
                            className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                            onClick={() => void revoke(k.id)}
                          >
                            Revoke key
                          </AlertDialogAction>
                        </AlertDialogFooter>
                      </AlertDialogContent>
                    </AlertDialog>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

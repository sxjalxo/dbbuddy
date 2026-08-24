// Unified AI provider management (Settings → AI Providers). Providers are records:
// each has an adapter (OpenAI-Compatible / Ollama), base URL, model, and optional
// key + fallback. One record per org is the active default. Adding a provider is
// creating a record here — there are no provider-specific sections anymore.
import { useEffect, useMemo, useState } from "react";
import { CheckCircle2, Copy, Loader2, Pencil, Plus, Sparkles, Trash2, Zap } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
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
import {
  aiProvidersApi,
  type AIAdapter,
  type AIProvider,
  type AIProviderInput,
  type AIProviderMetric,
} from "@/lib/api/platform";

function msg(e: unknown, fallback: string) {
  return e instanceof ApiError || e instanceof Error ? e.message : fallback;
}

const ADAPTER_LABEL: Record<AIAdapter, string> = {
  openai_compatible: "OpenAI-Compatible",
  ollama: "Ollama",
};

// A blank draft for the "Add provider" form.
const EMPTY_DRAFT: AIProviderInput = {
  name: "",
  adapter: "openai_compatible",
  base_url: "",
  model: "",
  api_key: "",
  enabled: true,
  fallback_provider_id: null,
};

export function AIProviders({ onActiveChange }: { onActiveChange?: () => void }) {
  const [providers, setProviders] = useState<AIProvider[]>([]);
  const [metrics, setMetrics] = useState<Record<string, AIProviderMetric>>({});
  const [loading, setLoading] = useState(true);
  const [editorOpen, setEditorOpen] = useState(false);
  // The record being edited, or null when creating a new one.
  const [editing, setEditing] = useState<AIProvider | null>(null);
  const [draft, setDraft] = useState<AIProviderInput>(EMPTY_DRAFT);
  const [saving, setSaving] = useState(false);
  // Per-row transient state keyed by provider id.
  const [testing, setTesting] = useState<string | null>(null);

  async function load() {
    try {
      setProviders(await aiProvidersApi.list());
    } catch (e) {
      toast.error(msg(e, "Could not load AI providers."));
    } finally {
      setLoading(false);
    }
    // Metrics are best-effort observability — never block the list on them.
    try {
      const m = await aiProvidersApi.metrics();
      setMetrics(Object.fromEntries(m.providers.map((p) => [p.name, p])));
    } catch {
      /* ignore */
    }
  }

  useEffect(() => {
    void load();
  }, []);

  // Candidate fallback records: any other record (a provider can't fall back to
  // itself). Empty string = "None".
  const fallbackOptions = useMemo(
    () => providers.filter((p) => !editing || p.id !== editing.id),
    [providers, editing],
  );

  const enabledProviders = useMemo(() => providers.filter((p) => p.enabled), [providers]);
  const activeId = providers.find((p) => p.is_active)?.id ?? "";

  async function activateById(id: string) {
    const p = providers.find((x) => x.id === id);
    if (p) await activate(p);
  }

  function openCreate() {
    setEditing(null);
    setDraft(EMPTY_DRAFT);
    setEditorOpen(true);
  }

  function openEdit(p: AIProvider) {
    setEditing(p);
    setDraft({
      name: p.name,
      adapter: p.adapter,
      base_url: p.base_url ?? "",
      model: p.model,
      api_key: "", // blank = keep the stored key
      enabled: p.enabled,
      fallback_provider_id: p.fallback_provider_id,
    });
    setEditorOpen(true);
  }

  async function save() {
    if (!draft.name.trim() || !draft.model.trim()) {
      toast.error("Name and model are required.");
      return;
    }
    if (draft.adapter === "openai_compatible" && !draft.base_url?.trim()) {
      toast.error("OpenAI-Compatible providers need a Base URL.");
      return;
    }
    setSaving(true);
    try {
      // Only send api_key when non-empty (blank keeps the stored key on edit).
      const body: Partial<AIProviderInput> = {
        name: draft.name.trim(),
        adapter: draft.adapter,
        base_url:
          draft.adapter === "ollama" ? draft.base_url?.trim() || null : draft.base_url?.trim(),
        model: draft.model.trim(),
        enabled: draft.enabled,
        fallback_provider_id: draft.fallback_provider_id || null,
      };
      if (draft.api_key && draft.api_key.trim()) body.api_key = draft.api_key.trim();

      if (editing) {
        await aiProvidersApi.update(editing.id, body);
        toast.success("Provider updated.");
      } else {
        await aiProvidersApi.create(body as AIProviderInput);
        toast.success("Provider added.");
      }
      setEditorOpen(false);
      await load();
    } catch (e) {
      toast.error(msg(e, "Could not save the provider."));
    } finally {
      setSaving(false);
    }
  }

  async function activate(p: AIProvider) {
    try {
      await aiProvidersApi.activate(p.id);
      await load();
      onActiveChange?.();
      toast.success(`“${p.name}” is now the active provider.`);
    } catch (e) {
      toast.error(msg(e, "Could not activate the provider."));
    }
  }

  async function duplicate(p: AIProvider) {
    try {
      await aiProvidersApi.duplicate(p.id);
      await load();
      toast.success("Provider duplicated — try a different model on the same key.");
    } catch (e) {
      toast.error(msg(e, "Could not duplicate the provider."));
    }
  }

  async function remove(p: AIProvider) {
    try {
      await aiProvidersApi.remove(p.id);
      await load();
      toast.success("Provider removed.");
    } catch (e) {
      toast.error(msg(e, "Could not remove the provider."));
    }
  }

  async function test(p: AIProvider) {
    setTesting(p.id);
    try {
      const r = await aiProvidersApi.test(p.id);
      if (r.ok) toast.success(`${p.name}: connection OK (${r.model}).`);
      else toast.error(`${p.name}: ${r.error ?? "connection failed."}`);
    } catch (e) {
      toast.error(msg(e, "Connection test failed."));
    } finally {
      setTesting(null);
    }
  }

  return (
    <div className="rounded-xl border border-border bg-card/80 p-6">
      <div className="mb-4 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <Sparkles className="h-4 w-4 text-muted-foreground" />
          <h2 className="text-sm font-semibold">AI Providers</h2>
        </div>
        <Button size="sm" className="gap-1.5" onClick={openCreate}>
          <Plus className="h-3.5 w-3.5" /> Add Provider
        </Button>
      </div>
      <p className="mb-4 text-xs text-muted-foreground">
        Configure any AI provider as a record. OpenAI-Compatible covers OpenAI, NVIDIA, OpenRouter,
        Groq, Together, Azure and more — just set the Base URL and Model. The active provider labels
        your schema; an optional fallback takes over if it's unreachable.
      </p>

      {/* Quick "Default AI Provider" selector — the org's active/default record. */}
      {enabledProviders.length > 0 && (
        <div className="mb-4 flex items-center justify-between gap-3 rounded-lg border border-border/60 bg-background/40 p-3">
          <div>
            <div className="text-sm font-medium">Default AI Provider</div>
            <div className="text-[11px] text-muted-foreground">
              Used to label your schema on Analyze. Switching takes effect immediately.
            </div>
          </div>
          <Select value={activeId} onValueChange={(v) => void activateById(v)}>
            <SelectTrigger className="w-[220px]">
              <SelectValue placeholder="Select a provider" />
            </SelectTrigger>
            <SelectContent>
              {enabledProviders.map((p) => (
                <SelectItem key={p.id} value={p.id}>
                  {p.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      )}

      {loading ? (
        <div className="flex items-center gap-2 py-4 text-xs text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading providers…
        </div>
      ) : providers.length === 0 ? (
        <p className="py-3 text-xs text-muted-foreground">
          No providers yet. Add one to enable AI-enhanced schema labeling.
        </p>
      ) : (
        <ul className="divide-y divide-border/60">
          {providers.map((p) => {
            const fallbackName = p.fallback_provider_id
              ? providers.find((x) => x.id === p.fallback_provider_id)?.name
              : null;
            return (
              <li
                key={p.id}
                className={`flex flex-wrap items-center justify-between gap-2 py-2.5 ${p.enabled ? "" : "opacity-60"}`}
              >
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="truncate text-sm font-medium">{p.name}</span>
                    {p.is_active && (
                      <Badge className="gap-1 text-[10px]">
                        <CheckCircle2 className="h-3 w-3" /> Active
                      </Badge>
                    )}
                    <Badge variant="secondary" className="text-[10px]">
                      {ADAPTER_LABEL[p.adapter]}
                    </Badge>
                    {!p.enabled && (
                      <Badge variant="outline" className="text-[10px]">
                        Disabled
                      </Badge>
                    )}
                    {p.has_key && !p.credentials_ok && (
                      <Badge
                        variant="outline"
                        className="text-[10px] border-red-500/40 text-red-400"
                      >
                        Key needs re-entry
                      </Badge>
                    )}
                    {metrics[p.name] && metrics[p.name].circuit_state !== "closed" && (
                      <Badge
                        variant="outline"
                        className="text-[10px] border-amber-500/40 text-amber-400"
                      >
                        Circuit {metrics[p.name].circuit_state === "open" ? "open" : "probing"}
                        {metrics[p.name].circuit_cooldown_s > 0
                          ? ` · ${Math.ceil(metrics[p.name].circuit_cooldown_s)}s`
                          : ""}
                      </Badge>
                    )}
                  </div>
                  <div className="mt-0.5 text-[11px] text-muted-foreground">
                    {p.model}
                    {p.base_url ? ` · ${p.base_url}` : ""}
                    {fallbackName ? ` · fallback → ${fallbackName}` : ""}
                  </div>
                  {metrics[p.name] && metrics[p.name].calls > 0 && (
                    <div className="mt-0.5 text-[11px] text-muted-foreground/80">
                      {Math.round(metrics[p.name].avg_latency_ms)} ms avg · {metrics[p.name].calls}{" "}
                      calls
                      {metrics[p.name].retries > 0 ? ` · ${metrics[p.name].retries} retries` : ""}
                      {metrics[p.name].failovers > 0
                        ? ` · ${metrics[p.name].failovers} failovers`
                        : ""}
                      {metrics[p.name].rate_limited > 0
                        ? ` · ${metrics[p.name].rate_limited} rate-limited`
                        : ""}
                      {metrics[p.name].skipped > 0 ? ` · ${metrics[p.name].skipped} skipped` : ""}
                    </div>
                  )}
                </div>

                <div className="flex items-center gap-1">
                  {!p.is_active && p.enabled && (
                    <Button
                      size="sm"
                      variant="outline"
                      className="h-7 gap-1"
                      title="Set as active"
                      onClick={() => void activate(p)}
                    >
                      <Zap className="h-3.5 w-3.5" /> Set active
                    </Button>
                  )}
                  <Button
                    size="icon"
                    variant="ghost"
                    className="h-7 w-7"
                    title="Test connection"
                    disabled={testing === p.id}
                    onClick={() => void test(p)}
                  >
                    {testing === p.id ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <Zap className="h-3.5 w-3.5" />
                    )}
                  </Button>
                  <Button
                    size="icon"
                    variant="ghost"
                    className="h-7 w-7"
                    title="Edit"
                    onClick={() => openEdit(p)}
                  >
                    <Pencil className="h-3.5 w-3.5" />
                  </Button>
                  <Button
                    size="icon"
                    variant="ghost"
                    className="h-7 w-7"
                    title="Duplicate"
                    onClick={() => void duplicate(p)}
                  >
                    <Copy className="h-3.5 w-3.5" />
                  </Button>
                  <AlertDialog>
                    <AlertDialogTrigger asChild>
                      <Button
                        size="icon"
                        variant="ghost"
                        className="h-7 w-7 text-destructive hover:text-destructive"
                        title="Delete"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    </AlertDialogTrigger>
                    <AlertDialogContent>
                      <AlertDialogHeader>
                        <AlertDialogTitle>Delete “{p.name}”?</AlertDialogTitle>
                        <AlertDialogDescription>
                          {p.is_active
                            ? "This is the active provider. After deleting it, pick a new active provider or AI labeling falls back to the built-in default."
                            : "This provider configuration will be removed. Records that fall back to it will have that fallback cleared."}
                        </AlertDialogDescription>
                      </AlertDialogHeader>
                      <AlertDialogFooter>
                        <AlertDialogCancel>Cancel</AlertDialogCancel>
                        <AlertDialogAction
                          className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                          onClick={() => void remove(p)}
                        >
                          Delete provider
                        </AlertDialogAction>
                      </AlertDialogFooter>
                    </AlertDialogContent>
                  </AlertDialog>
                </div>
              </li>
            );
          })}
        </ul>
      )}

      {/* Create / edit editor */}
      <Dialog open={editorOpen} onOpenChange={setEditorOpen}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>{editing ? "Edit provider" : "Add provider"}</DialogTitle>
            <DialogDescription>
              The same editor for every provider. The API key is stored encrypted and never shown
              again.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4 py-1">
            <div className="space-y-1.5">
              <Label>Name</Label>
              <Input
                value={draft.name}
                onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                placeholder="e.g. Production OpenRouter"
              />
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <Label>Adapter</Label>
                <Select
                  value={draft.adapter}
                  onValueChange={(v: AIAdapter) => setDraft({ ...draft, adapter: v })}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="openai_compatible">OpenAI-Compatible</SelectItem>
                    <SelectItem value="ollama">Ollama (local)</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label>Model</Label>
                <Input
                  value={draft.model}
                  onChange={(e) => setDraft({ ...draft, model: e.target.value })}
                  placeholder={
                    draft.adapter === "ollama" ? "qwen2.5-coder:7b" : "anthropic/claude-sonnet-4"
                  }
                />
              </div>
            </div>

            <div className="space-y-1.5">
              <Label>
                Base URL{" "}
                {draft.adapter === "ollama" && (
                  <span className="text-muted-foreground">(optional)</span>
                )}
              </Label>
              <Input
                value={draft.base_url ?? ""}
                onChange={(e) => setDraft({ ...draft, base_url: e.target.value })}
                placeholder={
                  draft.adapter === "ollama"
                    ? "http://127.0.0.1:11434"
                    : "https://openrouter.ai/api/v1"
                }
              />
            </div>

            {draft.adapter !== "ollama" && (
              <div className="space-y-1.5">
                <Label>API Key</Label>
                <Input
                  type="password"
                  value={draft.api_key ?? ""}
                  onChange={(e) => setDraft({ ...draft, api_key: e.target.value })}
                  autoComplete="off"
                  spellCheck={false}
                  placeholder={
                    editing?.has_key
                      ? "••••••••  (leave blank to keep)"
                      : "Paste the provider API key"
                  }
                />
              </div>
            )}

            <div className="space-y-1.5">
              <Label>
                Fallback provider <span className="text-muted-foreground">(optional)</span>
              </Label>
              <Select
                value={draft.fallback_provider_id ?? "none"}
                onValueChange={(v) =>
                  setDraft({ ...draft, fallback_provider_id: v === "none" ? null : v })
                }
              >
                <SelectTrigger>
                  <SelectValue placeholder="None" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">None</SelectItem>
                  {fallbackOptions.map((p) => (
                    <SelectItem key={p.id} value={p.id}>
                      {p.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-[11px] text-muted-foreground">
                Used only if this provider is unreachable (timeout, error, rate limit).
              </p>
            </div>

            <div className="flex items-center justify-between">
              <div>
                <div className="text-sm font-medium">Enabled</div>
                <div className="text-[11px] text-muted-foreground">
                  Disabled providers can't be activated or used as a fallback.
                </div>
              </div>
              <Switch
                checked={draft.enabled}
                onCheckedChange={(v) => setDraft({ ...draft, enabled: v })}
              />
            </div>
          </div>

          <DialogFooter>
            <Button variant="outline" onClick={() => setEditorOpen(false)} disabled={saving}>
              Cancel
            </Button>
            <Button onClick={() => void save()} disabled={saving} className="gap-1.5">
              {saving && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              {editing ? "Save changes" : "Add provider"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

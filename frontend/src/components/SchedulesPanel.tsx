// Schedules (Milestone 6). Analyst surface for background jobs: scheduled report
// refreshes and context rebuilds, with Run-now, enable/disable, and run history.
import { useCallback, useEffect, useState } from "react";
import { Clock, Loader2, Play, Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ApiError } from "@/lib/api/client";
import {
  connectionsApi,
  jobsApi,
  reportsApi,
  type ApiConnection,
  type ApiJob,
  type ApiReport,
} from "@/lib/api/platform";

function errMsg(e: unknown, fallback: string) {
  return e instanceof ApiError || e instanceof Error ? e.message : fallback;
}

function StatusBadge({ status }: { status: string | null }) {
  if (!status) return <span className="text-xs text-muted-foreground">never run</span>;
  const tone =
    status === "success"
      ? "text-green-500"
      : status === "error"
        ? "text-destructive"
        : "text-muted-foreground";
  return <span className={`text-xs font-medium ${tone}`}>{status}</span>;
}

export function SchedulesPanel() {
  const [jobs, setJobs] = useState<ApiJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [createOpen, setCreateOpen] = useState(false);
  const [runningId, setRunningId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setJobs(await jobsApi.list());
    } catch (e) {
      toast.error("Failed to load schedules", { description: errMsg(e, "") });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function runNow(job: ApiJob) {
    setRunningId(job.id);
    try {
      const run = await jobsApi.run(job.id);
      if (run.status === "success") toast.success(run.message || "Job ran successfully");
      else toast.error(run.message || "Job failed");
      await refresh();
    } catch (e) {
      toast.error("Could not run job", { description: errMsg(e, "") });
    } finally {
      setRunningId(null);
    }
  }

  async function toggle(job: ApiJob) {
    try {
      await jobsApi.update(job.id, { enabled: !job.enabled });
      await refresh();
    } catch (e) {
      toast.error("Could not update schedule", { description: errMsg(e, "") });
    }
  }

  async function remove(job: ApiJob) {
    try {
      await jobsApi.remove(job.id);
      await refresh();
    } catch (e) {
      toast.error("Could not delete schedule", { description: errMsg(e, "") });
    }
  }

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-y-auto px-8 py-6">
      <div className="mb-6 flex items-start justify-between gap-4 animate-fade-down">
        <div>
          <h1 className="font-display text-2xl font-semibold tracking-tight">Schedules</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Run report refreshes and context rebuilds automatically — or on demand.
          </p>
        </div>
        <Button size="sm" className="gap-1.5" onClick={() => setCreateOpen(true)}>
          <Plus className="h-4 w-4" /> New schedule
        </Button>
      </div>

      {loading ? (
        <div className="grid flex-1 place-items-center text-muted-foreground">
          <Loader2 className="h-6 w-6 animate-spin" />
        </div>
      ) : jobs.length === 0 ? (
        <div className="flex flex-1 items-center justify-center animate-fade-in">
          <div className="text-center">
            <Clock className="mx-auto h-12 w-12 text-muted-foreground/40 animate-float" />
            <p className="mt-4 text-sm text-muted-foreground">No schedules yet</p>
          </div>
        </div>
      ) : (
        <div className="space-y-3">
          {jobs.map((job, i) => (
            <div
              key={job.id}
              style={{ animationDelay: `${i * 60}ms` }}
              className="rounded-xl border border-border bg-card/80 p-4 animate-fade-up transition-all duration-300 hover:border-primary/30 hover:shadow-[0_8px_24px_-10px_oklch(0.92_0.03_240/0.2)]"
            >
              <div className="flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <p className="flex items-center gap-2 text-sm font-medium">
                    {job.name}
                    <Badge variant="secondary" className="text-[10px] uppercase">
                      {job.job_type === "report_refresh" ? "report" : "context"}
                    </Badge>
                    {!job.enabled && (
                      <Badge variant="outline" className="text-[10px]">
                        paused
                      </Badge>
                    )}
                  </p>
                  <p className="mt-0.5 text-[11px] text-muted-foreground">
                    {describeSchedule(job)} · last run: <StatusBadge status={job.last_status} />
                    {job.last_error ? ` — ${job.last_error}` : ""}
                  </p>
                </div>
                <div className="flex items-center gap-1">
                  <Button
                    size="sm"
                    variant="outline"
                    className="h-8 gap-1.5 text-xs"
                    disabled={runningId === job.id}
                    onClick={() => void runNow(job)}
                  >
                    {runningId === job.id ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <Play className="h-3.5 w-3.5" />
                    )}
                    Run now
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    className="h-8 text-xs"
                    onClick={() => void toggle(job)}
                  >
                    {job.enabled ? "Pause" : "Resume"}
                  </Button>
                  <Button
                    size="icon"
                    variant="ghost"
                    className="h-8 w-8 text-destructive hover:text-destructive"
                    onClick={() => void remove(job)}
                    title="Delete schedule"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      <CreateScheduleDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        onCreated={() => void refresh()}
      />
    </div>
  );
}

function describeSchedule(job: ApiJob): string {
  const c = job.schedule_config || {};
  if (job.schedule_kind === "manual") return "manual only";
  if (job.schedule_kind === "interval") return `every ${(c.minutes as number) ?? 60} min`;
  if (job.schedule_kind === "daily") return `daily at ${pad(c.hour)}:${pad(c.minute)}`;
  if (job.schedule_kind === "weekly")
    return `weekly (${c.day_of_week ?? "mon"}) at ${pad(c.hour)}:${pad(c.minute)}`;
  return job.schedule_kind;
}
function pad(v: unknown): string {
  return String(v ?? 0).padStart(2, "0");
}

function CreateScheduleDialog({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onCreated: () => void;
}) {
  const [name, setName] = useState("");
  const [type, setType] = useState<"report_refresh" | "context_rebuild">("report_refresh");
  const [target, setTarget] = useState("");
  const [kind, setKind] = useState<"manual" | "daily" | "weekly" | "interval">("daily");
  const [hour, setHour] = useState("6");
  const [reports, setReports] = useState<ApiReport[]>([]);
  const [connections, setConnections] = useState<ApiConnection[]>([]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    void reportsApi
      .list()
      .then(setReports)
      .catch(() => setReports([]));
    void connectionsApi
      .list()
      .then(setConnections)
      .catch(() => setConnections([]));
  }, [open]);

  const targets =
    type === "report_refresh"
      ? reports.map((r) => ({ id: r.id, label: r.title }))
      : connections.map((c) => ({ id: c.id, label: c.name }));

  async function submit() {
    if (!name.trim() || !target) {
      toast.error("Name and a target are required");
      return;
    }
    const schedule_config =
      kind === "interval"
        ? { minutes: 60 }
        : kind === "manual"
          ? null
          : { hour: Number(hour) || 6, minute: 0, day_of_week: "mon" };
    setBusy(true);
    try {
      await jobsApi.create({
        name: name.trim(),
        job_type: type,
        target_ref: target,
        schedule_kind: kind,
        schedule_config,
      });
      toast.success("Schedule created");
      setName("");
      setTarget("");
      onOpenChange(false);
      onCreated();
    } catch (e) {
      toast.error("Could not create schedule", { description: errMsg(e, "") });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New schedule</DialogTitle>
          <DialogDescription>
            Automate a report refresh or a query-context rebuild.
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label className="text-xs">Name</Label>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Nightly sales refresh"
            />
          </div>
          <div className="flex gap-3">
            <div className="flex flex-1 flex-col gap-1.5">
              <Label className="text-xs">Type</Label>
              <Select
                value={type}
                onValueChange={(v) => {
                  setType(v as typeof type);
                  setTarget("");
                }}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="report_refresh">Report refresh</SelectItem>
                  <SelectItem value="context_rebuild">Context rebuild</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="flex flex-1 flex-col gap-1.5">
              <Label className="text-xs">Target</Label>
              <Select value={target} onValueChange={setTarget}>
                <SelectTrigger>
                  <SelectValue placeholder="Select…" />
                </SelectTrigger>
                <SelectContent>
                  {targets.map((t) => (
                    <SelectItem key={t.id} value={t.id}>
                      {t.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
          <div className="flex gap-3">
            <div className="flex flex-1 flex-col gap-1.5">
              <Label className="text-xs">Schedule</Label>
              <Select value={kind} onValueChange={(v) => setKind(v as typeof kind)}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="manual">Manual only</SelectItem>
                  <SelectItem value="interval">Hourly</SelectItem>
                  <SelectItem value="daily">Daily</SelectItem>
                  <SelectItem value="weekly">Weekly (Mon)</SelectItem>
                </SelectContent>
              </Select>
            </div>
            {(kind === "daily" || kind === "weekly") && (
              <div className="flex w-24 flex-col gap-1.5">
                <Label className="text-xs">Hour (UTC)</Label>
                <Input
                  type="number"
                  min={0}
                  max={23}
                  value={hour}
                  onChange={(e) => setHour(e.target.value)}
                />
              </div>
            )}
          </div>
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button disabled={busy} onClick={() => void submit()} className="gap-2">
            {busy && <Loader2 className="h-4 w-4 animate-spin" />} Create
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

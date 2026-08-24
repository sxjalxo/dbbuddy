// "Pin to dashboard" — the second thing an analyst can do with a customized
// chart, alongside Publish. Publish sends the chart to clients as-is; pinning
// places it inside a dashboard, where it gains a position and a description.
//
// The two are independent: a chart can be published *and* pinned, because they
// answer different questions ("show clients this chart" vs "this chart is part of
// this story").
import { useCallback, useEffect, useState } from "react";
import { Loader2, Pin, Plus } from "lucide-react";
import { toast } from "sonner";

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
import { dashboardsApi, type ApiDashboard } from "@/lib/api/platform";

export function PinToDashboard({
  chartId,
  chartTitle,
  onPinned,
}: {
  chartId: string;
  chartTitle: string;
  onPinned?: (dashboard: ApiDashboard) => void;
}) {
  const [open, setOpen] = useState(false);
  const [dashboards, setDashboards] = useState<ApiDashboard[]>([]);
  const [loading, setLoading] = useState(false);
  const [pinning, setPinning] = useState<string | null>(null);
  const [newTitle, setNewTitle] = useState("");

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    dashboardsApi
      .list()
      .then(setDashboards)
      .catch((err) =>
        toast.error(err instanceof Error ? err.message : "Could not load dashboards."),
      )
      .finally(() => setLoading(false));
  }, [open]);

  const pin = useCallback(
    async (dashboardId: string) => {
      setPinning(dashboardId);
      try {
        const updated = await dashboardsApi.pin(dashboardId, { chart_id: chartId });
        toast.success(`Pinned to “${updated.title}”.`);
        setOpen(false);
        onPinned?.(updated);
      } catch (err) {
        toast.error(err instanceof Error ? err.message : "Could not pin the chart.");
      } finally {
        setPinning(null);
      }
    },
    [chartId, onPinned],
  );

  const createAndPin = useCallback(async () => {
    const title = newTitle.trim();
    if (!title) return;
    setPinning("new");
    try {
      const created = await dashboardsApi.create({ title });
      const updated = await dashboardsApi.pin(created.id, { chart_id: chartId });
      toast.success(`Created “${title}” and pinned this chart.`);
      setNewTitle("");
      setOpen(false);
      onPinned?.(updated);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Could not create the dashboard.");
    } finally {
      setPinning(null);
    }
  }, [newTitle, chartId, onPinned]);

  return (
    <>
      <Button
        size="sm"
        variant="outline"
        className="h-7 gap-1.5 text-xs"
        onClick={() => setOpen(true)}
      >
        <Pin className="h-3 w-3" /> Pin to dashboard
      </Button>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Pin “{chartTitle}”</DialogTitle>
            <DialogDescription>
              Add this chart to a dashboard. It stays live — the dashboard re-runs it against the
              database each time it is opened.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-3">
            {loading ? (
              <div className="flex justify-center py-4">
                <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
              </div>
            ) : dashboards.length > 0 ? (
              <div className="max-h-56 space-y-1.5 overflow-y-auto">
                {dashboards.map((d) => {
                  const already = d.items.some((i) => i.chart_id === chartId);
                  return (
                    <button
                      key={d.id}
                      type="button"
                      disabled={pinning !== null}
                      onClick={() => void pin(d.id)}
                      className="flex w-full items-center justify-between rounded-lg border border-border px-3 py-2 text-left transition-colors hover:border-primary/50 disabled:opacity-50"
                    >
                      <span className="min-w-0">
                        <span className="block truncate text-xs font-medium">{d.title}</span>
                        <span className="text-[10px] text-muted-foreground">
                          {d.items.length} chart{d.items.length === 1 ? "" : "s"}
                          {already && " · already pinned"}
                        </span>
                      </span>
                      {pinning === d.id && <Loader2 className="h-3 w-3 animate-spin" />}
                    </button>
                  );
                })}
              </div>
            ) : (
              <p className="text-xs text-muted-foreground">
                You have no dashboards yet. Create one below.
              </p>
            )}

            <form
              className="flex items-center gap-2 border-t border-border pt-3"
              onSubmit={(e) => {
                e.preventDefault();
                void createAndPin();
              }}
            >
              <Input
                value={newTitle}
                onChange={(e) => setNewTitle(e.target.value)}
                placeholder="New dashboard name…"
                className="h-8 text-xs"
              />
              <Button
                type="submit"
                size="sm"
                variant="outline"
                className="h-8 shrink-0 gap-1.5 text-xs"
                disabled={pinning !== null || !newTitle.trim()}
              >
                {pinning === "new" ? (
                  <Loader2 className="h-3 w-3 animate-spin" />
                ) : (
                  <Plus className="h-3 w-3" />
                )}
                Create &amp; pin
              </Button>
            </form>
          </div>

          <DialogFooter>
            <Button size="sm" variant="ghost" className="text-xs" onClick={() => setOpen(false)}>
              Cancel
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

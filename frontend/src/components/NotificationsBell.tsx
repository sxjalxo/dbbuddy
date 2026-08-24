// Header notifications indicator (Milestone 6). Shows an unread count and a
// dropdown of recent notifications (job completions/failures). Polls the unread
// count periodically so background-job results surface without a refresh.
import { useCallback, useEffect, useState } from "react";
import { Bell, Check } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { notificationsApi, type ApiNotification } from "@/lib/api/platform";

export function NotificationsBell() {
  const [count, setCount] = useState(0);
  const [items, setItems] = useState<ApiNotification[]>([]);
  const [open, setOpen] = useState(false);

  const loadCount = useCallback(async () => {
    try {
      setCount((await notificationsApi.unreadCount()).count);
    } catch {
      /* ignore transient errors */
    }
  }, []);

  useEffect(() => {
    void loadCount();
    const t = setInterval(() => void loadCount(), 30_000); // poll every 30s
    return () => clearInterval(t);
  }, [loadCount]);

  async function openPanel(next: boolean) {
    setOpen(next);
    if (next) {
      try {
        setItems(await notificationsApi.list());
      } catch {
        /* ignore */
      }
    }
  }

  async function markAll() {
    await notificationsApi.markAllRead().catch(() => {});
    setItems((prev) => prev.map((n) => ({ ...n, read: true })));
    setCount(0);
  }

  const dot = (level: string) =>
    level === "error"
      ? "bg-destructive"
      : level === "success"
        ? "bg-green-500"
        : "bg-muted-foreground";

  return (
    <Popover open={open} onOpenChange={openPanel}>
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="group relative h-8 w-8"
          aria-label="Notifications"
        >
          <Bell
            className={`h-4 w-4 transition-transform duration-200 group-hover:scale-110 ${count > 0 ? "group-hover:rotate-12" : ""}`}
          />
          {count > 0 && (
            <span className="absolute -right-0.5 -top-0.5 grid h-4 min-w-4 place-items-center rounded-full bg-destructive px-1 text-[10px] font-semibold text-destructive-foreground animate-scale-in">
              {count > 9 ? "9+" : count}
            </span>
          )}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80 p-0">
        <div className="flex items-center justify-between border-b border-border px-3 py-2">
          <span className="text-sm font-semibold">Notifications</span>
          {items.some((n) => !n.read) && (
            <button
              className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
              onClick={() => void markAll()}
            >
              <Check className="h-3 w-3" /> Mark all read
            </button>
          )}
        </div>
        <div className="max-h-80 overflow-y-auto">
          {items.length === 0 ? (
            <p className="px-3 py-6 text-center text-xs text-muted-foreground">
              No notifications yet.
            </p>
          ) : (
            items.map((n, i) => (
              <div
                key={n.id}
                style={{ animationDelay: `${i * 40}ms` }}
                className={`flex gap-2 border-b border-border/50 px-3 py-2.5 animate-fade-up transition-colors duration-200 hover:bg-muted/40 ${n.read ? "opacity-60" : ""}`}
              >
                <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${dot(n.level)}`} />
                <div className="min-w-0">
                  <p className="truncate text-xs font-medium">{n.title}</p>
                  {n.body && <p className="text-[11px] text-muted-foreground">{n.body}</p>}
                  <p className="mt-0.5 text-[10px] text-muted-foreground/60">
                    {new Date(n.created_at).toLocaleString()}
                  </p>
                </div>
              </div>
            ))
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}

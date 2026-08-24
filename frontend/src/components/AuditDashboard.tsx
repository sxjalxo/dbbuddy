// Audit dashboard (Milestone 4). Read-only view over the audit trail for users
// with audit:read — platform admins see every org, org admins see their own.
// Summary cards + a filterable (entity/action) table + CSV export.
import { useCallback, useEffect, useMemo, useState } from "react";
import { Activity, AlertTriangle, Download, Loader2, Users } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { auditApi, type AuditEntry, type AuditSummary } from "@/lib/api/platform";

const ALL = "__all__";

export function AuditDashboard() {
  const [summary, setSummary] = useState<AuditSummary | null>(null);
  const [rows, setRows] = useState<AuditEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [entity, setEntity] = useState<string>(ALL);
  const [action, setAction] = useState<string>(ALL);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [s, list] = await Promise.all([
        auditApi.summary(30),
        auditApi.list({
          entity_type: entity === ALL ? undefined : entity,
          action: action === ALL ? undefined : action,
          limit: 200,
        }),
      ]);
      setSummary(s);
      setRows(list);
    } finally {
      setLoading(false);
    }
  }, [entity, action]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const entityOptions = useMemo(() => Object.keys(summary?.by_entity ?? {}).sort(), [summary]);
  const actionOptions = useMemo(() => Object.keys(summary?.by_action ?? {}).sort(), [summary]);

  function exportCsv() {
    const cols = ["created_at", "actor_email", "entity_type", "action", "entity_id", "ip_address"];
    const esc = (v: unknown) => `"${String(v ?? "").replace(/"/g, '""')}"`;
    const csv = [
      cols.join(","),
      ...rows.map((r) => cols.map((c) => esc((r as Record<string, unknown>)[c])).join(",")),
    ].join("\n");
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `audit-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="space-y-4">
      {/* Summary cards */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Card
          icon={<Activity className="h-4 w-4" />}
          label={`Events (${summary?.window_days ?? 30}d)`}
          value={summary?.total}
          delayMs={0}
        />
        <Card
          icon={<Users className="h-4 w-4" />}
          label="Active users"
          value={summary?.active_users}
          delayMs={70}
        />
        <Card
          icon={<AlertTriangle className="h-4 w-4 text-amber-500" />}
          label="Failed logins"
          value={summary?.recent_failures}
          warn={(summary?.recent_failures ?? 0) > 0}
          delayMs={140}
        />
        <Card
          icon={<Activity className="h-4 w-4" />}
          label="Top action"
          value={topKey(summary?.by_action)}
          delayMs={210}
        />
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-2 animate-fade-up delay-100">
        <Select value={entity} onValueChange={setEntity}>
          <SelectTrigger className="h-8 w-[150px]">
            <SelectValue placeholder="Entity" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>All entities</SelectItem>
            {entityOptions.map((e) => (
              <SelectItem key={e} value={e}>
                {e}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={action} onValueChange={setAction}>
          <SelectTrigger className="h-8 w-[150px]">
            <SelectValue placeholder="Action" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>All actions</SelectItem>
            {actionOptions.map((a) => (
              <SelectItem key={a} value={a}>
                {a}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Button
          variant="outline"
          size="sm"
          className="ml-auto gap-1.5"
          onClick={exportCsv}
          disabled={!rows.length}
        >
          <Download className="h-4 w-4" /> Export CSV
        </Button>
      </div>

      {/* Table */}
      <div className="rounded-xl border border-border animate-fade-up delay-200">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Time</TableHead>
              <TableHead>Actor</TableHead>
              <TableHead>Entity</TableHead>
              <TableHead>Action</TableHead>
              <TableHead>Target / detail</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {loading ? (
              <TableRow>
                <TableCell colSpan={5} className="py-8 text-center text-muted-foreground">
                  <Loader2 className="mx-auto h-5 w-5 animate-spin" />
                </TableCell>
              </TableRow>
            ) : rows.length === 0 ? (
              <TableRow>
                <TableCell colSpan={5} className="py-8 text-center text-sm text-muted-foreground">
                  No events match these filters.
                </TableCell>
              </TableRow>
            ) : (
              rows.map((r) => (
                <TableRow key={r.id}>
                  <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                    {new Date(r.created_at).toLocaleString()}
                  </TableCell>
                  <TableCell className="text-sm">{r.actor_email ?? "—"}</TableCell>
                  <TableCell>
                    {r.entity_type && (
                      <Badge variant="secondary" className="text-[10px] uppercase">
                        {r.entity_type}
                      </Badge>
                    )}
                  </TableCell>
                  <TableCell className="font-mono text-xs">
                    {r.action === "login_failed" ? (
                      <span className="text-amber-500">{r.action}</span>
                    ) : (
                      r.action
                    )}
                  </TableCell>
                  <TableCell className="max-w-xs truncate font-mono text-[11px] text-muted-foreground">
                    {r.detail ? JSON.stringify(r.detail) : (r.entity_id ?? "")}
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}

function Card({
  icon,
  label,
  value,
  warn,
  delayMs = 0,
}: {
  icon: React.ReactNode;
  label: string;
  value: number | string | undefined;
  warn?: boolean;
  delayMs?: number;
}) {
  return (
    <div
      className="rounded-xl border border-border bg-card p-4 animate-fade-up transition-all duration-300 hover:-translate-y-0.5 hover:border-primary/30 hover:shadow-[0_8px_24px_-10px_oklch(0.92_0.03_240/0.25)]"
      style={{ animationDelay: `${delayMs}ms` }}
    >
      <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
        {icon} {label}
      </div>
      <div
        className={`mt-1 font-display text-2xl font-semibold animate-count-up ${warn ? "text-amber-500" : ""}`}
        style={{ animationDelay: `${delayMs + 120}ms` }}
      >
        {value ?? "—"}
      </div>
    </div>
  );
}

function topKey(rec: Record<string, number> | undefined): string {
  if (!rec) return "—";
  const entries = Object.entries(rec);
  if (!entries.length) return "—";
  return entries.sort((a, b) => b[1] - a[1])[0][0];
}

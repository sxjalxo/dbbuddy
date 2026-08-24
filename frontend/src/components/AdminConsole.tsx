// Admin workspace (Milestone 2). Platform admins (org:manage) manage every
// organization and user; org admins (user:manage only) manage their own org's
// members. This is the role-branched destination for admins from AuthGate —
// the analyst never sees it, and plain users get RoleLanding instead.
import { useCallback, useEffect, useState } from "react";
import { Building2, Loader2, LogOut, Plus, ScrollText, Shield, Trash2, Users } from "lucide-react";
import { toast } from "sonner";

import { AuditDashboard } from "@/components/AuditDashboard";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Toaster } from "@/components/ui/sonner";
import { ApiError, setForbiddenHandler } from "@/lib/api/client";
import { adminUsersApi, orgsApi, type AdminUser, type ApiOrg } from "@/lib/api/platform";
import { useAuth } from "@/lib/auth";

const PLATFORM_ROLES = ["admin", "org_admin", "analyst", "user"];
const ORG_ADMIN_ROLES = ["analyst", "user"];

function errMessage(e: unknown, fallback: string): string {
  return e instanceof ApiError ? e.message : e instanceof Error ? e.message : fallback;
}

export function AdminConsole() {
  const { user, logout, hasPermission } = useAuth();
  const isPlatformAdmin = hasPermission("org:manage");
  const canReadAudit = hasPermission("audit:read");
  const assignableRoles = isPlatformAdmin ? PLATFORM_ROLES : ORG_ADMIN_ROLES;

  const [orgs, setOrgs] = useState<ApiOrg[]>([]);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [newOrg, setNewOrg] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<AdminUser | null>(null);

  const orgName = useCallback(
    (id: string) => orgs.find((o) => o.id === id)?.name ?? id.slice(0, 8),
    [orgs],
  );

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [u, o] = await Promise.all([
        adminUsersApi.list(),
        orgsApi.list().catch(() => [] as ApiOrg[]),
      ]);
      setUsers(u);
      setOrgs(o);
    } catch (e) {
      toast.error("Failed to load admin data", { description: errMessage(e, "Try again.") });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    setForbiddenHandler((detail) =>
      toast.error("You don't have access to do that", { description: detail }),
    );
    return () => setForbiddenHandler(null);
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function createOrg() {
    const name = newOrg.trim();
    if (!name) return;
    try {
      await orgsApi.create(name);
      setNewOrg("");
      toast.success(`Organization "${name}" created`);
      await refresh();
    } catch (e) {
      toast.error("Could not create organization", { description: errMessage(e, "") });
    }
  }

  async function deleteOrg(o: ApiOrg) {
    try {
      await orgsApi.remove(o.id);
      toast.success(`Deleted "${o.name}"`);
      await refresh();
    } catch (e) {
      toast.error("Could not delete organization", { description: errMessage(e, "") });
    }
  }

  async function setUserRole(u: AdminUser, role: string) {
    try {
      await adminUsersApi.update(u.id, { roles: [role] });
      toast.success(`${u.email} is now ${role}`);
      await refresh();
    } catch (e) {
      toast.error("Could not change role", { description: errMessage(e, "") });
    }
  }

  async function moveUserOrg(u: AdminUser, organization_id: string) {
    try {
      await adminUsersApi.update(u.id, { organization_id });
      toast.success(`Moved ${u.email}`);
      await refresh();
    } catch (e) {
      toast.error("Could not move user", { description: errMessage(e, "") });
    }
  }

  async function toggleActive(u: AdminUser) {
    try {
      await adminUsersApi.update(u.id, { is_active: !u.is_active });
      await refresh();
    } catch (e) {
      toast.error("Could not update user", { description: errMessage(e, "") });
    }
  }

  async function deleteUser(u: AdminUser) {
    // Close the dialog *before* the async refresh: refresh() swaps the table for a
    // loading spinner, unmounting this user's row. If the dialog were still closing
    // then, Radix would try to restore focus to the now-gone trigger and leave
    // `pointer-events: none` stuck on <body>, freezing the page. Closing first (and
    // onCloseAutoFocus below) avoids that race.
    setPendingDelete(null);
    try {
      await adminUsersApi.remove(u.id);
      toast.success(`Deleted ${u.email}`);
      await refresh();
    } catch (e) {
      toast.error("Could not delete user", { description: errMessage(e, "") });
    }
  }

  return (
    <div className="flex min-h-screen w-full flex-col bg-background text-foreground">
      <header className="flex items-center justify-between border-b border-border px-6 py-3 animate-fade-down">
        <div className="flex items-center gap-3">
          <div className="grid h-9 w-9 place-items-center rounded-xl brand-gradient transition-transform duration-200 hover:scale-105">
            <Shield className="h-5 w-5 text-primary-foreground" />
          </div>
          <div>
            <h1 className="font-display text-lg font-semibold tracking-tight">Admin Console</h1>
            <p className="text-xs text-muted-foreground">
              {isPlatformAdmin ? "Platform administrator" : "Organization administrator"} ·{" "}
              {user?.email}
            </p>
          </div>
        </div>
        <Button variant="outline" size="sm" className="gap-2" onClick={() => void logout()}>
          <LogOut className="h-4 w-4" /> Sign out
        </Button>
      </header>

      <main className="mx-auto w-full max-w-5xl flex-1 px-6 py-6 animate-fade-up">
        <Tabs defaultValue="users">
          <TabsList>
            <TabsTrigger value="users" className="gap-1.5">
              <Users className="h-3.5 w-3.5" /> Users
            </TabsTrigger>
            {isPlatformAdmin && (
              <TabsTrigger value="orgs" className="gap-1.5">
                <Building2 className="h-3.5 w-3.5" /> Organizations
              </TabsTrigger>
            )}
            {canReadAudit && (
              <TabsTrigger value="audit" className="gap-1.5">
                <ScrollText className="h-3.5 w-3.5" /> Audit
              </TabsTrigger>
            )}
          </TabsList>

          {/* Users */}
          <TabsContent value="users" className="mt-4">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-sm font-semibold">
                {isPlatformAdmin ? "All users" : "Team members"}
              </h2>
              <Button size="sm" className="gap-1.5" onClick={() => setCreateOpen(true)}>
                <Plus className="h-4 w-4" /> New user
              </Button>
            </div>
            <div className="rounded-xl border border-border animate-fade-up delay-100">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Email</TableHead>
                    <TableHead>Role</TableHead>
                    {isPlatformAdmin && <TableHead>Organization</TableHead>}
                    <TableHead>Status</TableHead>
                    <TableHead className="text-right">Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {loading ? (
                    <TableRow>
                      <TableCell
                        colSpan={isPlatformAdmin ? 5 : 4}
                        className="py-8 text-center text-muted-foreground"
                      >
                        <Loader2 className="mx-auto h-5 w-5 animate-spin" />
                      </TableCell>
                    </TableRow>
                  ) : (
                    users.map((u) => {
                      const primaryRole = u.roles[0] ?? "user";
                      const self = u.id === user?.id;
                      return (
                        <TableRow key={u.id}>
                          <TableCell>
                            <div className="font-medium">{u.full_name || u.email}</div>
                            {u.full_name && (
                              <div className="text-xs text-muted-foreground">{u.email}</div>
                            )}
                          </TableCell>
                          <TableCell>
                            <Select
                              value={primaryRole}
                              onValueChange={(v) => void setUserRole(u, v)}
                            >
                              <SelectTrigger className="h-8 w-[140px]">
                                <SelectValue />
                              </SelectTrigger>
                              <SelectContent>
                                {Array.from(new Set([...assignableRoles, primaryRole])).map((r) => (
                                  <SelectItem
                                    key={r}
                                    value={r}
                                    disabled={!assignableRoles.includes(r)}
                                  >
                                    {r}
                                  </SelectItem>
                                ))}
                              </SelectContent>
                            </Select>
                          </TableCell>
                          {isPlatformAdmin && (
                            <TableCell>
                              <Select
                                value={u.organization_id}
                                onValueChange={(v) => void moveUserOrg(u, v)}
                              >
                                <SelectTrigger className="h-8 w-[160px]">
                                  <SelectValue>{orgName(u.organization_id)}</SelectValue>
                                </SelectTrigger>
                                <SelectContent>
                                  {orgs.map((o) => (
                                    <SelectItem key={o.id} value={o.id}>
                                      {o.name}
                                    </SelectItem>
                                  ))}
                                </SelectContent>
                              </Select>
                            </TableCell>
                          )}
                          <TableCell>
                            <Button
                              variant={u.is_active ? "ghost" : "secondary"}
                              size="sm"
                              disabled={self}
                              title={self ? "You can't change your own status" : ""}
                              onClick={() => void toggleActive(u)}
                            >
                              <Badge variant={u.is_active ? "default" : "secondary"}>
                                {u.is_active ? "Active" : "Inactive"}
                              </Badge>
                            </Button>
                          </TableCell>
                          <TableCell className="text-right">
                            <Button
                              variant="ghost"
                              size="icon"
                              disabled={self}
                              title={self ? "You can't delete your own account" : "Delete user"}
                              onClick={() => setPendingDelete(u)}
                            >
                              <Trash2 className="h-4 w-4 text-destructive" />
                            </Button>
                          </TableCell>
                        </TableRow>
                      );
                    })
                  )}
                </TableBody>
              </Table>
            </div>
          </TabsContent>

          {/* Organizations (platform admin only) */}
          {isPlatformAdmin && (
            <TabsContent value="orgs" className="mt-4">
              <div className="mb-3 flex items-center gap-2">
                <Input
                  value={newOrg}
                  onChange={(e) => setNewOrg(e.target.value)}
                  placeholder="New organization name"
                  className="max-w-xs"
                  onKeyDown={(e) => e.key === "Enter" && void createOrg()}
                />
                <Button size="sm" className="gap-1.5" onClick={() => void createOrg()}>
                  <Plus className="h-4 w-4" /> Create
                </Button>
              </div>
              <div className="rounded-xl border border-border">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Name</TableHead>
                      <TableHead>Members</TableHead>
                      <TableHead className="text-right">Actions</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {orgs.map((o) => (
                      <TableRow key={o.id}>
                        <TableCell className="font-medium">
                          {o.name}
                          {o.is_default && (
                            <Badge variant="secondary" className="ml-2 text-[10px] uppercase">
                              default
                            </Badge>
                          )}
                          <div className="font-mono text-xs text-muted-foreground">/{o.slug}</div>
                        </TableCell>
                        <TableCell>{o.member_count}</TableCell>
                        <TableCell className="text-right">
                          <Button
                            variant="ghost"
                            size="icon"
                            disabled={o.is_default || o.member_count > 0}
                            title={
                              o.is_default
                                ? "The default organization can't be deleted"
                                : o.member_count > 0
                                  ? "Reassign members before deleting"
                                  : "Delete organization"
                            }
                            onClick={() => void deleteOrg(o)}
                          >
                            <Trash2 className="h-4 w-4 text-destructive" />
                          </Button>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            </TabsContent>
          )}

          {canReadAudit && (
            <TabsContent value="audit" className="mt-4">
              <AuditDashboard />
            </TabsContent>
          )}
        </Tabs>
      </main>

      <CreateUserDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        roles={assignableRoles}
        orgs={isPlatformAdmin ? orgs : []}
        onCreated={() => void refresh()}
      />
      <AlertDialog open={pendingDelete !== null} onOpenChange={(o) => !o && setPendingDelete(null)}>
        <AlertDialogContent onCloseAutoFocus={(e) => e.preventDefault()}>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete {pendingDelete?.email}?</AlertDialogTitle>
            <AlertDialogDescription>
              This permanently removes the account and everything it owns — connections, saved and
              published charts, query history, API keys, and scheduled jobs. This cannot be undone.
              To suspend access without deleting, use the Active/Inactive toggle instead.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={() => pendingDelete && void deleteUser(pendingDelete)}
            >
              Delete user
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
      <Toaster position="bottom-right" />
    </div>
  );
}

function CreateUserDialog({
  open,
  onOpenChange,
  roles,
  orgs,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  roles: string[];
  orgs: ApiOrg[];
  onCreated: () => void;
}) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [fullName, setFullName] = useState("");
  const [role, setRole] = useState(roles[0] ?? "analyst");
  const [orgId, setOrgId] = useState<string>("");
  const [busy, setBusy] = useState(false);

  async function submit() {
    if (!email.trim() || password.length < 8) {
      toast.error("Email and an 8+ character password are required");
      return;
    }
    setBusy(true);
    try {
      await adminUsersApi.create({
        email: email.trim(),
        password,
        full_name: fullName.trim() || null,
        role,
        organization_id: orgs.length ? orgId || orgs[0].id : null,
      });
      toast.success(`Created ${email.trim()}`);
      setEmail("");
      setPassword("");
      setFullName("");
      onOpenChange(false);
      onCreated();
    } catch (e) {
      toast.error("Could not create user", { description: errMessage(e, "") });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New user</DialogTitle>
          <DialogDescription>Create an account and assign a role.</DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label className="text-xs">Email</Label>
            <Input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="person@company.com"
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label className="text-xs">Temporary password</Label>
            <Input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="At least 8 characters"
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label className="text-xs">Full name (optional)</Label>
            <Input value={fullName} onChange={(e) => setFullName(e.target.value)} />
          </div>
          <div className="flex gap-3">
            <div className="flex flex-1 flex-col gap-1.5">
              <Label className="text-xs">Role</Label>
              <Select value={role} onValueChange={setRole}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {roles.map((r) => (
                    <SelectItem key={r} value={r}>
                      {r}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {orgs.length > 0 && (
              <div className="flex flex-1 flex-col gap-1.5">
                <Label className="text-xs">Organization</Label>
                <Select value={orgId || orgs[0].id} onValueChange={setOrgId}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {orgs.map((o) => (
                      <SelectItem key={o.id} value={o.id}>
                        {o.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}
          </div>
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button disabled={busy} onClick={() => void submit()} className="gap-2">
            {busy && <Loader2 className="h-4 w-4 animate-spin" />} Create user
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

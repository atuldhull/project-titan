"use client";

import { useState } from "react";
import { toast } from "sonner";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import {
  ResponsiveTable,
  type ResponsiveColumn,
} from "@/components/settings/responsive-table";
import { useCan } from "@/lib/auth";
import { errorMessage } from "@/lib/settings/errors";
import { useMembers, useRoles, useSetMemberRoles } from "@/lib/settings/hooks";
import { ApiError, type MemberOut } from "@titan/api-client";

/**
 * Settings → Members (Vol 4 §6.13).
 *
 * **The role picker reads the server.** §6.13 names owner/admin/developer/
 * viewer; `developer` does not exist anywhere in TITAN. The five roles the seed
 * actually creates are owner, admin, maintainer, operator and viewer, and
 * assigning a name outside that set is rejected — so the options come from
 * `GET /roles` and are never hard-coded here.
 *
 * **There is no invite flow.** No invitations table, no invite endpoint, no
 * mail transport of any kind — a user row is created by the seed script or by
 * an operator against the database. An "Invite" button would have nothing to
 * call, so the screen says how members really arrive instead.
 */
export function MembersScreen() {
  const members = useMembers();
  const roles = useRoles();
  const setRoles = useSetMemberRoles();
  const canWrite = useCan("member:write");

  // Which row is in flight. One `useMutation` serves every row, so its shared
  // `isPending` would disable all of them at once; the user id narrows the busy
  // state to the row that was actually changed.
  const [pending, setPending] = useState<string | null>(null);
  const [conflict, setConflict] = useState<string | null>(null);

  if (members.isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (members.isError || !members.data) {
    return (
      <p role="status" className="rounded-md border bg-card p-4 text-sm">
        Couldn’t load members. {errorMessage(members.error)}{" "}
        <button className="underline" onClick={() => void members.refetch()}>
          Retry
        </button>
      </p>
    );
  }

  const rows = members.data;
  const roleOptions = roles.data ?? [];
  // Without the role list there is nothing legitimate to offer, and guessing
  // the names is exactly what this screen exists to avoid — so the picker
  // degrades to read-only badges rather than to a wrong list.
  const editable = canWrite && roleOptions.length > 0;

  const changeRole = async (member: MemberOut, role: string) => {
    setConflict(null);
    setPending(member.user_id);
    try {
      await setRoles.mutateAsync({ userId: member.user_id, roles: [role] });
      toast.success(`${member.display_name} is now ${role}`);
    } catch (error) {
      // 409 is the last-owner guard. `owner` is the only bundle holding `*`,
      // and granting `owner` needs a permission only owner and admin carry —
      // so an org that loses its final owner can never get one back. Nothing
      // re-seeds a live database, which makes that state an unrecoverable
      // lockout rather than an inconvenience. Surface the server's own
      // sentence, inline and persistent, not just as a toast that expires.
      if (error instanceof ApiError && error.status === 409) {
        setConflict(errorMessage(error));
      }
      toast.error(errorMessage(error));
    } finally {
      setPending(null);
    }
  };

  // Declared once, rendered as a table at md+ and as cards below (§8). The
  // role picker is CONDITIONAL, which is exactly the drift a hand-written
  // second layout gets wrong: appended here, it appears in — or is absent
  // from — both layouts together.
  const columns: readonly ResponsiveColumn<MemberOut>[] = [
    {
      key: "member",
      header: "Member",
      primary: true,
      cell: (member) => member.display_name,
    },
    {
      key: "email",
      header: "Email",
      cell: (member) => (
        // `break-all`: an email is one unbreakable token, and at 343px a long
        // one is the single most likely thing on this screen to push the card
        // wider than the viewport.
        <span className="text-xs break-all text-muted-foreground">
          {member.email}
        </span>
      ),
    },
    {
      key: "roles",
      header: "Org roles",
      cell: (member) => {
        const memberRoles = member.roles ?? [];
        return (
          <div className="flex flex-wrap gap-1">
            {memberRoles.length === 0 ? (
              <span className="text-xs text-muted-foreground">none</span>
            ) : (
              memberRoles.map((role) => (
                <Badge key={role} variant="secondary">
                  {role}
                </Badge>
              ))
            )}
          </div>
        );
      },
    },
    {
      key: "grants",
      header: "Project grants",
      cell: (member) => {
        const grants = member.project_grants ?? [];
        return (
          <span className="text-xs text-muted-foreground">
            {grants.length === 0
              ? "—"
              : `${grants.length} project${grants.length === 1 ? "" : "s"}`}
          </span>
        );
      },
    },
    ...(editable
      ? [
          {
            key: "change-role",
            header: "Change role",
            cell: (member: MemberOut) => {
              const memberRoles = member.roles ?? [];
              return (
                <Select
                  // Empty when the member holds several org roles: the PUT
                  // REPLACES the org-wide set, so picking one would silently
                  // drop the others. Leaving it unselected makes that a
                  // deliberate act.
                  value={memberRoles.length === 1 ? memberRoles[0] : ""}
                  disabled={pending === member.user_id}
                  onValueChange={(role) => void changeRole(member, role)}
                >
                  {/* §8's 44px touch minimum below md; `size="sm"` is 32px. */}
                  <SelectTrigger
                    size="sm"
                    className="min-h-11 md:min-h-0"
                    aria-label={`Org role for ${member.display_name}`}
                  >
                    <SelectValue placeholder="Set one role…" />
                  </SelectTrigger>
                  <SelectContent>
                    {roleOptions.map((role) => (
                      <SelectItem key={role} value={role}>
                        {role}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              );
            },
          },
        ]
      : []),
  ];

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle>Members</CardTitle>
          <CardDescription>
            Org-wide roles. Project-scoped grants are made from the project
            itself and are only counted here.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {conflict ? (
            <Alert variant="destructive">
              <AlertTitle>Role not changed</AlertTitle>
              <AlertDescription>
                <p>{conflict}</p>
              </AlertDescription>
            </Alert>
          ) : null}

          {rows.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No members. That normally means the database has not been seeded.
            </p>
          ) : (
            <ResponsiveTable
              rows={rows}
              rowKey={(member) => member.user_id}
              columns={columns}
              caption="Org members and their roles"
            />
          )}

          {!canWrite ? (
            <p className="text-xs text-muted-foreground">
              Read-only — changing roles needs the owner or admin role. Hiding
              the control is a convenience, not the control: the server
              re-checks every write.
            </p>
          ) : roles.isError ? (
            <p className="text-xs text-muted-foreground">
              Couldn’t load the role list, so editing is off rather than
              offering names that might not exist. {errorMessage(roles.error)}{" "}
              <button
                className="underline"
                onClick={() => void roles.refetch()}
              >
                Retry
              </button>
            </p>
          ) : null}

          <p className="text-xs text-muted-foreground">
            Choosing a role replaces that member’s entire org-wide set — it is a
            replace, not an add.
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Adding people</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            Members are provisioned out-of-band today. TITAN has no invitation
            record and no mail delivery, so accounts are created by the seed
            script or by an operator, and then given a role here.
          </p>
        </CardContent>
      </Card>
    </div>
  );
}

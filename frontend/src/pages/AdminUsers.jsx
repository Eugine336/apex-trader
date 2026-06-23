import { useCallback, useEffect, useState } from "react";

import { getUsers, updateUser } from "../api/admin";
import { extractError } from "../api/client";
import ConfirmDialog from "../components/ConfirmDialog";
import { useAuth } from "../context/AuthContext";
import { formatDateTime } from "../utils/format";

export default function AdminUsers() {
  const { user: me } = useAuth();
  const [users, setUsers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [actionLoading, setActionLoading] = useState(false);
  // pending = { userId, field: "is_active"|"is_admin", value, label } | null
  const [pending, setPending] = useState(null);

  const load = useCallback(async () => {
    try {
      const rows = await getUsers();
      setUsers(rows);
      setError("");
    } catch (err) {
      setError(extractError(err, "Failed to load users"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const applyUpdate = async (userId, field, value) => {
    setActionLoading(true);
    try {
      await updateUser(userId, { [field]: value });
      await load();
    } catch (err) {
      setError(extractError(err, "Update failed"));
    } finally {
      setActionLoading(false);
      setPending(null);
    }
  };

  // Destructive toggles (disable account, revoke admin) require confirmation.
  const requestToggle = (target, field, nextValue) => {
    const destructive =
      (field === "is_active" && nextValue === false) ||
      (field === "is_admin" && nextValue === false);
    if (destructive) {
      setPending({
        userId: target.id,
        field,
        value: nextValue,
        label:
          field === "is_active"
            ? `Disable ${target.email}?`
            : `Revoke admin from ${target.email}?`,
      });
    } else {
      applyUpdate(target.id, field, nextValue);
    }
  };

  if (loading) {
    return <div className="animate-pulse text-sm text-gray-400">Loading users…</div>;
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold text-gray-100">User Management</h1>

      {error && (
        <div className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-400">
          {error}
        </div>
      )}

      <div className="apex-scroll overflow-x-auto rounded-lg border border-gray-700">
        <table className="min-w-full divide-y divide-gray-700 text-sm">
          <thead className="bg-gray-800">
            <tr className="text-left text-xs uppercase tracking-wider text-gray-400">
              <th className="px-4 py-3">ID</th>
              <th className="px-4 py-3">Email</th>
              <th className="px-4 py-3">Role</th>
              <th className="px-4 py-3">Status</th>
              <th className="px-4 py-3">Created</th>
              <th className="px-4 py-3 text-right">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-700">
            {users.map((u, i) => {
              const isSelf = me?.id === u.id;
              return (
                <tr
                  key={u.id}
                  className={i % 2 === 0 ? "bg-gray-800" : "bg-gray-750"}
                >
                  <td className="px-4 py-3 tabular-nums text-gray-400">{u.id}</td>
                  <td className="px-4 py-3 font-medium text-gray-100">
                    {u.email}
                    {isSelf && (
                      <span className="ml-2 text-xs text-gray-500">(you)</span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    {u.is_admin ? (
                      <span className="inline-block rounded px-2 py-0.5 text-xs font-semibold bg-emerald-500/15 text-emerald-400 border border-emerald-500/30">
                        ADMIN
                      </span>
                    ) : (
                      <span className="inline-block rounded px-2 py-0.5 text-xs font-semibold bg-gray-600/30 text-gray-300 border border-gray-600">
                        USER
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    {u.is_active ? (
                      <span className="text-emerald-400">Active</span>
                    ) : (
                      <span className="text-red-400">Disabled</span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-gray-400">
                    {formatDateTime(u.created_at)}
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex justify-end gap-2">
                      <button
                        type="button"
                        disabled={isSelf || actionLoading}
                        onClick={() =>
                          requestToggle(u, "is_active", !u.is_active)
                        }
                        className="rounded-md border border-gray-600 px-3 py-1.5 text-xs font-medium text-gray-300 transition-colors duration-200 hover:bg-gray-700 disabled:opacity-40"
                      >
                        {u.is_active ? "Disable" : "Enable"}
                      </button>
                      <button
                        type="button"
                        disabled={isSelf || actionLoading}
                        onClick={() => requestToggle(u, "is_admin", !u.is_admin)}
                        className="rounded-md border border-gray-600 px-3 py-1.5 text-xs font-medium text-gray-300 transition-colors duration-200 hover:bg-gray-700 disabled:opacity-40"
                      >
                        {u.is_admin ? "Revoke Admin" : "Make Admin"}
                      </button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <ConfirmDialog
        open={pending !== null}
        title={pending?.label || "Are you sure?"}
        message="This change takes effect immediately."
        confirmLabel="Confirm"
        destructive
        loading={actionLoading}
        onConfirm={() =>
          pending && applyUpdate(pending.userId, pending.field, pending.value)
        }
        onCancel={() => setPending(null)}
      />
    </div>
  );
}

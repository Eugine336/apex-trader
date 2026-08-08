import { useCallback, useEffect, useState } from "react";

import { extractError } from "../api/client";
import {
  deleteCredentials,
  listCredentials,
  setDeriv,
  setMT5,
} from "../api/broker";
import ConfirmDialog from "../components/ConfirmDialog";

function ConfiguredBadge({ configured }) {
  return configured ? (
    <span className="inline-flex items-center gap-1.5 text-sm font-medium text-emerald-400">
      <span className="h-2 w-2 rounded-full bg-emerald-500" /> Configured
    </span>
  ) : (
    <span className="inline-flex items-center gap-1.5 text-sm font-medium text-gray-500">
      <span className="h-2 w-2 rounded-full bg-gray-500" /> Not configured
    </span>
  );
}

export default function BrokerSettings() {
  const [creds, setCreds] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [deleteTarget, setDeleteTarget] = useState(null);
  const [deleting, setDeleting] = useState(false);

  // MT5 form
  const [mt5, setMt5State] = useState({ login: "", password: "", server: "", label: "" });
  const [mt5Saving, setMt5Saving] = useState(false);

  // Deriv form
  const [deriv, setDerivState] = useState({
    access_token: "",
    app_id: "",
    account_type: "demo",
    client_id: "",
    label: "",
  });
  const [derivSaving, setDerivSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const rows = await listCredentials();
      setCreds(rows);
      setError("");
    } catch (err) {
      setError(extractError(err, "Failed to load broker credentials"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const mt5Cred = creds.find((c) => c.broker_type === "mt5");
  const derivCred = creds.find((c) => c.broker_type === "deriv");

  const flash = (msg) => {
    setNotice(msg);
    setTimeout(() => setNotice(""), 4000);
  };

  const saveMt5 = async (e) => {
    e.preventDefault();
    setError("");
    setMt5Saving(true);
    try {
      await setMT5(mt5);
      flash("MT5 credentials saved");
      setMt5State((s) => ({ ...s, password: "" }));
      await load();
    } catch (err) {
      setError(extractError(err, "Failed to save MT5 credentials"));
    } finally {
      setMt5Saving(false);
    }
  };

  const saveDeriv = async (e) => {
    e.preventDefault();
    setError("");
    setDerivSaving(true);
    try {
      await setDeriv(deriv);
      flash("Deriv credentials saved");
      setDerivState((s) => ({ ...s, access_token: "" }));
      await load();
    } catch (err) {
      setError(extractError(err, "Failed to save Deriv credentials"));
    } finally {
      setDerivSaving(false);
    }
  };

  const confirmDelete = async () => {
    setDeleting(true);
    try {
      await deleteCredentials(deleteTarget);
      flash(`${deleteTarget.toUpperCase()} credentials deleted`);
      await load();
    } catch (err) {
      setError(extractError(err, "Failed to delete credentials"));
    } finally {
      setDeleting(false);
      setDeleteTarget(null);
    }
  };

  const inputClass =
    "w-full rounded-md border border-gray-600 bg-gray-700 px-3 py-2 text-sm text-white placeholder-gray-500 focus:border-emerald-500 focus:outline-none focus:ring-1 focus:ring-emerald-500";

  if (loading) {
    return <div className="animate-pulse text-sm text-gray-400">Loading…</div>;
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold text-gray-100">Broker Settings</h1>

      {error && (
        <div className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-400">
          {error}
        </div>
      )}
      {notice && (
        <div className="rounded-md border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-400">
          {notice}
        </div>
      )}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        {/* MT5 */}
        <form
          onSubmit={saveMt5}
          className="space-y-4 rounded-lg border border-gray-700 bg-gray-800 p-6 shadow-lg"
        >
          <div className="flex items-center justify-between">
            <h2 className="text-lg font-semibold text-gray-100">MetaTrader 5</h2>
            <ConfiguredBadge configured={!!mt5Cred} />
          </div>

          {mt5Cred?.masked && (
            <div className="rounded-md border border-gray-700 bg-gray-900/40 px-3 py-2 text-xs text-gray-400">
              Stored:{" "}
              {Object.entries(mt5Cred.masked)
                .map(([k, v]) => `${k}=${v}`)
                .join("  ·  ")}
            </div>
          )}

          <div>
            <label className="mb-1 block text-sm font-medium text-gray-300">Login</label>
            <input
              type="number"
              required
              value={mt5.login}
              onChange={(e) => setMt5State((s) => ({ ...s, login: e.target.value }))}
              placeholder="12345678"
              className={inputClass}
            />
          </div>
          <div>
            <label className="mb-1 block text-sm font-medium text-gray-300">Password</label>
            <input
              type="password"
              required
              value={mt5.password}
              onChange={(e) => setMt5State((s) => ({ ...s, password: e.target.value }))}
              placeholder="••••••••"
              className={inputClass}
            />
          </div>
          <div>
            <label className="mb-1 block text-sm font-medium text-gray-300">Server</label>
            <input
              type="text"
              required
              value={mt5.server}
              onChange={(e) => setMt5State((s) => ({ ...s, server: e.target.value }))}
              placeholder="MetaQuotes-Demo"
              className={inputClass}
            />
          </div>
          <div>
            <label className="mb-1 block text-sm font-medium text-gray-300">
              Label <span className="text-gray-500">(optional)</span>
            </label>
            <input
              type="text"
              value={mt5.label}
              onChange={(e) => setMt5State((s) => ({ ...s, label: e.target.value }))}
              placeholder="My demo account"
              className={inputClass}
            />
          </div>

          <div className="flex gap-3">
            <button
              type="submit"
              disabled={mt5Saving}
              className="rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white transition-colors duration-200 hover:bg-emerald-700 disabled:opacity-50"
            >
              {mt5Saving ? "Saving…" : mt5Cred ? "Update" : "Save"}
            </button>
            {mt5Cred && (
              <button
                type="button"
                onClick={() => setDeleteTarget("mt5")}
                className="rounded-md bg-red-600 px-4 py-2 text-sm font-medium text-white transition-colors duration-200 hover:bg-red-700"
              >
                Delete
              </button>
            )}
          </div>
        </form>

        {/* Deriv */}
        <form
          onSubmit={saveDeriv}
          className="space-y-4 rounded-lg border border-gray-700 bg-gray-800 p-6 shadow-lg"
        >
          <div className="flex items-center justify-between">
            <h2 className="text-lg font-semibold text-gray-100">Deriv</h2>
            <ConfiguredBadge configured={!!derivCred} />
          </div>

          {derivCred?.masked && (
            <div className="rounded-md border border-gray-700 bg-gray-900/40 px-3 py-2 text-xs text-gray-400">
              Stored:{" "}
              {Object.entries(derivCred.masked)
                .map(([k, v]) => `${k}=${v}`)
                .join("  ·  ")}
            </div>
          )}

          <div>
            <label className="mb-1 block text-sm font-medium text-gray-300">
              Access Token
            </label>
            <input
              type="password"
              required
              value={deriv.access_token}
              onChange={(e) =>
                setDerivState((s) => ({ ...s, access_token: e.target.value }))
              }
              placeholder="API token"
              className={inputClass}
            />
          </div>
          <div>
            <label className="mb-1 block text-sm font-medium text-gray-300">App ID</label>
            <input
              type="text"
              required
              value={deriv.app_id}
              onChange={(e) => setDerivState((s) => ({ ...s, app_id: e.target.value }))}
              placeholder="1089"
              className={inputClass}
            />
          </div>
          <div>
            <label className="mb-1 block text-sm font-medium text-gray-300">
              Account Type
            </label>
            <select
              value={deriv.account_type}
              onChange={(e) =>
                setDerivState((s) => ({ ...s, account_type: e.target.value }))
              }
              className={inputClass}
            >
              <option value="demo">Demo</option>
              <option value="real">Real</option>
            </select>
          </div>
          <div>
            <label className="mb-1 block text-sm font-medium text-gray-300">
              Client ID <span className="text-gray-500">(optional)</span>
            </label>
            <input
              type="text"
              value={deriv.client_id}
              onChange={(e) =>
                setDerivState((s) => ({ ...s, client_id: e.target.value }))
              }
              placeholder=""
              className={inputClass}
            />
          </div>
          <div>
            <label className="mb-1 block text-sm font-medium text-gray-300">
              Label <span className="text-gray-500">(optional)</span>
            </label>
            <input
              type="text"
              value={deriv.label}
              onChange={(e) => setDerivState((s) => ({ ...s, label: e.target.value }))}
              placeholder="My Deriv account"
              className={inputClass}
            />
          </div>

          <div className="flex gap-3">
            <button
              type="submit"
              disabled={derivSaving}
              className="rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white transition-colors duration-200 hover:bg-emerald-700 disabled:opacity-50"
            >
              {derivSaving ? "Saving…" : derivCred ? "Update" : "Save"}
            </button>
            {derivCred && (
              <button
                type="button"
                onClick={() => setDeleteTarget("deriv")}
                className="rounded-md bg-red-600 px-4 py-2 text-sm font-medium text-white transition-colors duration-200 hover:bg-red-700"
              >
                Delete
              </button>
            )}
          </div>
        </form>
      </div>

      <ConfirmDialog
        open={deleteTarget !== null}
        title={`Delete ${String(deleteTarget || "").toUpperCase()} credentials?`}
        message="Your stored credentials will be removed. You can re-add them at any time."
        confirmLabel="Delete"
        destructive
        loading={deleting}
        onConfirm={confirmDelete}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  );
}

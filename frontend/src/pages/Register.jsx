import { useMemo, useState } from "react";
import { Link, Navigate, useNavigate } from "react-router-dom";

import { extractError } from "../api/client";
import { useAuth } from "../context/AuthContext";

function passwordStrength(pw) {
  let score = 0;
  if (pw.length >= 8) score += 1;
  if (pw.length >= 12) score += 1;
  if (/[A-Z]/.test(pw) && /[a-z]/.test(pw)) score += 1;
  if (/\d/.test(pw)) score += 1;
  if (/[^A-Za-z0-9]/.test(pw)) score += 1;
  return Math.min(score, 4);
}

const STRENGTH = [
  { label: "Too short", color: "bg-red-500", text: "text-red-400" },
  { label: "Weak", color: "bg-red-500", text: "text-red-400" },
  { label: "Fair", color: "bg-yellow-500", text: "text-yellow-400" },
  { label: "Good", color: "bg-blue-500", text: "text-blue-400" },
  { label: "Strong", color: "bg-emerald-500", text: "text-emerald-400" },
];

export default function Register() {
  const { register, isAuthenticated, loading } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const strength = useMemo(() => passwordStrength(password), [password]);

  if (!loading && isAuthenticated) {
    return <Navigate to="/" replace />;
  }

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError("");
    if (password.length < 8) {
      setError("Password must be at least 8 characters");
      return;
    }
    if (password !== confirm) {
      setError("Passwords do not match");
      return;
    }
    setSubmitting(true);
    try {
      await register(email, password);
      navigate("/", { replace: true });
    } catch (err) {
      setError(extractError(err, "Registration failed"));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-gray-900 p-4">
      <div className="w-full max-w-md">
        <div className="mb-8 text-center">
          <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-lg bg-emerald-600 text-xl font-bold text-white">
            A
          </div>
          <h1 className="text-2xl font-semibold text-gray-100">APEX Trader</h1>
          <p className="mt-1 text-sm text-gray-400">Create your account</p>
        </div>

        <form
          onSubmit={handleSubmit}
          className="space-y-4 rounded-lg border border-gray-700 bg-gray-800 p-6 shadow-lg"
        >
          {error && (
            <div className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-400">
              {error}
            </div>
          )}

          <div>
            <label className="mb-1 block text-sm font-medium text-gray-300">
              Email
            </label>
            <input
              type="email"
              required
              autoComplete="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full rounded-md border border-gray-600 bg-gray-700 px-3 py-2 text-white placeholder-gray-500 focus:border-emerald-500 focus:outline-none focus:ring-1 focus:ring-emerald-500"
              placeholder="you@example.com"
            />
          </div>

          <div>
            <label className="mb-1 block text-sm font-medium text-gray-300">
              Password
            </label>
            <input
              type="password"
              required
              autoComplete="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full rounded-md border border-gray-600 bg-gray-700 px-3 py-2 text-white placeholder-gray-500 focus:border-emerald-500 focus:outline-none focus:ring-1 focus:ring-emerald-500"
              placeholder="At least 8 characters"
            />
            {password && (
              <div className="mt-2">
                <div className="flex gap-1">
                  {[0, 1, 2, 3].map((i) => (
                    <div
                      key={i}
                      className={`h-1 flex-1 rounded-full ${
                        i < strength ? STRENGTH[strength].color : "bg-gray-600"
                      }`}
                    />
                  ))}
                </div>
                <p className={`mt-1 text-xs ${STRENGTH[strength].text}`}>
                  {STRENGTH[strength].label}
                </p>
              </div>
            )}
          </div>

          <div>
            <label className="mb-1 block text-sm font-medium text-gray-300">
              Confirm Password
            </label>
            <input
              type="password"
              required
              autoComplete="new-password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              className="w-full rounded-md border border-gray-600 bg-gray-700 px-3 py-2 text-white placeholder-gray-500 focus:border-emerald-500 focus:outline-none focus:ring-1 focus:ring-emerald-500"
              placeholder="Re-enter your password"
            />
          </div>

          <button
            type="submit"
            disabled={submitting}
            className="w-full rounded-md bg-emerald-600 px-4 py-2 font-medium text-white transition-colors duration-200 hover:bg-emerald-700 disabled:opacity-50"
          >
            {submitting ? "Creating account…" : "Create Account"}
          </button>

          <p className="text-center text-sm text-gray-400">
            Already have an account?{" "}
            <Link to="/login" className="text-emerald-400 hover:text-emerald-300">
              Login
            </Link>
          </p>
        </form>
      </div>
    </div>
  );
}

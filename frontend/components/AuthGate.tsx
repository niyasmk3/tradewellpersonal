"use client";

import { useEffect, useState } from "react";
import { api, AuthStatus, SignalHistoryRow } from "@/lib/api";
import { SignalHistoryPanel } from "./SignalHistoryPanel";

export function AuthGate({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Signal history is LOCAL data (card store + archive) — no Kite session
  // involved — so the login gate must not hide it. A card that expired while
  // you were logged out is exactly the record you came back to check
  // (the 22-Jul lesson: the best card ever left no trace).
  const [hist, setHist] = useState<{ rows: SignalHistoryRow[]; count: number } | null>(null);
  const [histErr, setHistErr] = useState<string | null>(null);

  const refresh = async () => {
    try {
      setStatus(await api.authStatus());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Cannot reach backend at " + "localhost:8000");
    }
  };

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 5000);
    return () => clearInterval(id);
  }, []);

  const authed = status?.authenticated === true;
  useEffect(() => {
    if (authed) return; // the dashboard's own panel takes over after login
    let stop = false;
    const load = () =>
      api
        // 30 days, not the dashboard's 7: this view exists for "what did I
        // miss while logged out", and a quiet week would render it empty.
        .signalHistory("NIFTY", 30)
        .then((d) => { if (!stop) { setHist(d); setHistErr(null); } })
        .catch((e) => { if (!stop) setHistErr(e instanceof Error ? e.message : String(e)); });
    load();
    const id = setInterval(load, 60000);
    return () => { stop = true; clearInterval(id); };
  }, [authed]);

  // Daily-login ergonomics: pasting the whole redirect URL is the common
  // mistake — pull the request_token out of it automatically.
  const onTokenInput = (v: string) => {
    const m = v.match(/[?&]request_token=([^&\s]+)/);
    setToken(m ? m[1] : v);
  };

  const submit = async () => {
    const t = token.trim();
    if (!t) return;
    setBusy(true);
    setError(null);
    try {
      const s = await api.createSession(t);
      setStatus(s);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Session failed");
    } finally {
      setBusy(false);
    }
  };

  if (status?.authenticated) return <>{children}</>;

  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-4 p-6">
      <div className="card w-full max-w-lg p-6">
        <div className="mb-1 flex items-center gap-2">
          <h1 className="text-lg font-semibold">Tradewell</h1>
          <span className="tag bg-panel2 text-muted">Kite login</span>
        </div>
        <p className="mb-4 text-sm text-muted">
          Kite access tokens expire every morning, so you log in once per trading day.
        </p>

        {status && !status.api_key_configured && (
          <div className="mb-4 rounded-md border border-bear/40 bg-bear/10 p-3 text-sm text-bear">
            <code>KITE_API_KEY</code> / <code>KITE_API_SECRET</code> are not set in{" "}
            <code>backend/.env</code>. Add them and restart the backend.
          </div>
        )}

        {status?.api_key_configured && (
          <ol className="space-y-4 text-sm">
            <li>
              <div className="mb-1 text-muted">1 · Open the Kite login page and sign in</div>
              {status.login_url ? (
                <a
                  href={status.login_url}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-block rounded-md bg-accent px-3 py-1.5 font-medium text-white hover:opacity-90"
                >
                  Open Kite login ↗
                </a>
              ) : (
                <span className="text-muted">login url unavailable</span>
              )}
            </li>
            <li>
              <div className="mb-1 text-muted">
                2 · After login you are redirected to your app URL. Copy the{" "}
                <code>request_token</code> value from that URL.
              </div>
            </li>
            <li>
              <div className="mb-1 text-muted">3 · Paste the request_token and connect</div>
              <div className="flex gap-2">
                <input
                  value={token}
                  onChange={(e) => onTokenInput(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && submit()}
                  placeholder="request_token — or paste the whole redirect URL"
                  className="flex-1 rounded-md border border-edge bg-panel2 px-3 py-1.5 font-mono text-sm outline-none focus:border-accent"
                />
                <button
                  onClick={submit}
                  disabled={busy || !token.trim()}
                  className="rounded-md bg-bull px-4 py-1.5 font-medium text-black disabled:opacity-40"
                >
                  {busy ? "Connecting…" : "Connect"}
                </button>
              </div>
            </li>
          </ol>
        )}

        {error && <div className="mt-4 text-sm text-bear">{error}</div>}
        {status?.message && !error && (
          <div className="mt-4 text-xs text-muted">{status.message}</div>
        )}
      </div>

      {/* Local data the gate has no business hiding. Live signals, the chain
          and the tape still need the login above. */}
      {status && (
        <div className="card w-full max-w-3xl p-4">
          <div className="mb-2 flex items-baseline gap-2">
            <h2 className="text-sm font-semibold">Signal history · NIFTY</h2>
            <span className="tag bg-panel2 text-[10px] text-muted">
              available without login — local records
            </span>
          </div>
          <div className="max-h-[24rem] overflow-auto">
            <SignalHistoryPanel data={hist} error={histErr} />
          </div>
        </div>
      )}
    </div>
  );
}

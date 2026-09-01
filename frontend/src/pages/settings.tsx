/**
 * Settings: what this product IS, operationally. No preference theater -
 * only facts about the local installation and the honest state of its
 * external connections. The setup checklist doubles as first-run docs.
 */

import { useCallback, useEffect, useState } from "react";
import { CheckCircle2, XCircle } from "lucide-react";
import { api, SetupNeeded } from "@/lib/api";
import { Badge, Card, CardHeader, Stat } from "@/components/ui";
import { SetupNeededBanner, LoadError } from "@/components/setup-needed";

type Phase =
  | { kind: "loading" }
  | { kind: "setup"; error: SetupNeeded }
  | { kind: "error"; error: Error }
  | { kind: "ready"; engineUp: boolean; hasDb: boolean; dbPath: string; stale: boolean };

function Row({ ok, title, body }: { ok: boolean; title: string; body: string }) {
  return (
    <div className="flex items-start gap-3 px-4 py-3">
      {ok ? (
        <CheckCircle2 size={16} className="mt-0.5 shrink-0 text-state-answerable" />
      ) : (
        <XCircle size={16} className="mt-0.5 shrink-0 text-state-error" />
      )}
      <div>
        <p className="text-sm font-medium">{title}</p>
        <p className="mono text-xs text-text-muted">{body}</p>
      </div>
    </div>
  );
}

export function SettingsPage() {
  const [phase, setPhase] = useState<Phase>({ kind: "loading" });

  const load = useCallback(async () => {
    setPhase({ kind: "loading" });
    try {
      const health = await api.health();
      let stale = false;
      let lastDrain: string | null = null;
      try {
        const s = await api.status();
        stale = s.stale_sync;
        lastDrain = s.last_drain_at;
      } catch {
        /* status needs a cursor row; a fresh DB legitimately lacks one */
      }
      setPhase({
        kind: "ready",
        engineUp: true,
        hasDb: health.has_db,
        dbPath: health.db_path,
        stale,
      });
      void lastDrain;
    } catch (e) {
      if (e instanceof SetupNeeded) setPhase({ kind: "setup", error: e });
      else if (e instanceof Error) setPhase({ kind: "error", error: e });
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="mx-auto max-w-3xl space-y-4 p-6">
      <h1 className="text-lg font-medium">Settings</h1>

      {phase.kind === "loading" && <Card className="p-6"><div className="h-24" /></Card>}
      {phase.kind === "setup" && <SetupNeededBanner error={phase.error} />}
      {phase.kind === "error" && <LoadError error={phase.error} />}

      {phase.kind === "ready" && (
        <>
          <Card>
            <CardHeader
              title="Setup"
              action={<Badge tone={phase.hasDb ? "answerable" : "insufficient"}>{phase.hasDb ? "READY" : "FIRST RUN"}</Badge>}
            />
            <Row
              ok={phase.engineUp}
              title="Engine running"
              body="the local API answered — this is it"
            />
            <Row
              ok={phase.hasDb}
              title="Local index"
              body={phase.dbPath}
            />
            <div className="border-t border-border-subtle px-4 py-3">
              <p className="text-sm font-medium">Google Drive connection</p>
              <p className="text-xs text-text-muted">
                Authorized once via <span className="mono text-xs">fenris drive auth</span> (PKCE,
                read-only). The refresh token lives in your OS keyring — never on disk.
                Disconnect revokes it for real.
              </p>
            </div>
            <div className="border-t border-border-subtle px-4 py-3">
              <p className="text-sm font-medium">Gemini API key</p>
              <p className="text-xs text-text-muted">
                Embeddings + generation. Set <span className="mono text-xs">GOOGLE_API_KEY</span> in
                the environment of the machine running the engine.
              </p>
            </div>
          </Card>

          <Card>
            <CardHeader title="Privacy" />
            <div className="grid grid-cols-3 divide-x divide-border-subtle">
              <Stat
                label="sync state"
                value={<Badge tone={phase.stale ? "stale" : "answerable"}>{phase.stale ? "STALE" : "FRESH"}</Badge>}
              />
              <Stat label="storage" value="your Drive" />
              <Stat label="index" value="local only" />
            </div>
            <p className="border-t border-border-subtle px-4 py-2 text-xs text-text-muted">
              Your files never leave your Drive. Fenris reads them (read-only scope),
              keeps a local search index, and sends only your questions to the
              Gemini API. Nothing else is transmitted, stored remotely, or shared.
            </p>
          </Card>

          <Card>
            <CardHeader title="Legal" />
            <div className="divide-y divide-border-subtle">
              {[
                { href: "/legal/terms-of-service", label: "Terms of Service" },
                { href: "/legal/privacy-policy", label: "Privacy Policy" },
                { href: "/legal/security-disclosure", label: "Security & Data Handling" },
              ].map((l) => (
                <a
                  key={l.href}
                  href={l.href}
                  target="_blank"
                  rel="noreferrer"
                  className="flex items-center justify-between px-4 py-3 text-sm text-text-muted transition-colors hover:bg-bg-overlay hover:text-accent"
                >
                  {l.label}
                  <span className="mono text-xs text-text-faint">↗</span>
                </a>
              ))}
            </div>
            <p className="border-t border-border-subtle px-4 py-2 text-xs text-text-faint">
              Version 1.0 · adapted from attorney-drafted templates (General Legal, CC0).
              Read them before relying on them.
            </p>
          </Card>
        </>
      )}
    </div>
  );
}

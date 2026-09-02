/**
 * First-run onboarding: the doctor findings turned into a guided path.
 * Each step is a failing check with its fix; the final step is a real
 * question the user can click to see the product answer with citations.
 */

import { useCallback, useEffect, useState } from "react";
import { ArrowRight, CheckCircle2, Circle, ExternalLink } from "lucide-react";
import { api, type DoctorReport } from "@/lib/api";
import { Badge, Button, Card, CardHeader } from "@/components/ui";
import { cn } from "@/lib/utils";

const SAMPLE_QUESTION = "What is this app and what's in my Drive so far?";

type StepStatus = "done" | "todo" | "blocked";

interface Step {
  key: string;
  title: string;
  body: string;
  status: StepStatus;
  fix?: React.ReactNode;
}

export function Onboarding({ onDone }: { onDone: () => void }) {
  const [doctor, setDoctor] = useState<DoctorReport | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [syncTried, setSyncTried] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setDoctor(await api.doctor());
    } catch {
      setDoctor(null);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const byName = (n: string) => doctor?.checks.find((c) => c.name === n);

  const steps: Step[] = [
    {
      key: "engine",
      title: "Engine running",
      body: "You're looking at it - the local API answered.",
      status: "done",
    },
    {
      key: "keyring",
      title: "OS keyring available",
      body: (byName("OS keyring")?.detail ?? "checking…"),
      status: byName("OS keyring")?.ok ? "done" : "todo",
      fix: !byName("OS keyring")?.ok && (
        <span className="mono text-xs text-state-stale">
          install/unlock gnome-keyring (Linux) or run inside a desktop session
        </span>
      ),
    },
    {
      key: "apikey",
      title: "Set your Gemini API key",
      body: "Embeddings + answers. Free at aistudio.google.com/apikey — then export GOOGLE_API_KEY and restart the engine.",
      status: byName("Gemini API key")?.ok ? "done" : "todo",
      fix: !byName("Gemini API key")?.ok && (
        <a
          className="mono inline-flex items-center gap-1 text-xs text-accent hover:text-accent-hover"
          href="https://aistudio.google.com/apikey"
          target="_blank"
          rel="noreferrer"
        >
          get a key <ExternalLink size={11} />
        </a>
      ),
    },
    {
      key: "drive",
      title: "Connect your Drive (read-only)",
      body: "One terminal command opens a browser; you approve read-only access. The token lives in your OS keyring.",
      status: byName("Drive connection")?.ok ? "done" : "todo",
      fix: !byName("Drive connection")?.ok && (
        <span className="mono text-xs text-accent">fenris drive auth</span>
      ),
    },
    {
      key: "sync",
      title: "Run the first sync",
      body: "Index your Drive locally. Files stay on Drive; this builds the search projection. First sync takes real time - paced against Google's quota.",
      status: syncTried ? "todo" : "todo",
    },
  ];

  const runFirstSync = useCallback(async () => {
    setSyncing(true);
    try {
      await api.sync();
      setSyncTried(true);
      onDone(); // refresh the dashboard counts
    } finally {
      setSyncing(false);
      void refresh();
    }
  }, [onDone, refresh]);

  const allReady = steps[0].status === "done"
    && steps[1].status === "done" && steps[2].status === "done"
    && steps[3].status === "done";

  return (
    <div className="mx-auto max-w-3xl space-y-4 p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-medium">Welcome to Fenris</h1>
        <Badge tone="accent">FIRST RUN</Badge>
      </div>

      <Card>
        <CardHeader
          title="Set up in four steps"
          action={
            <Button variant="ghost" onClick={() => void refresh()}>
              re-check
            </Button>
          }
        />
        <div className="divide-y divide-border-subtle">
          {steps.map((s) => (
            <div key={s.key} className="flex items-start gap-3 px-4 py-3.5">
              {s.status === "done" ? (
                <CheckCircle2 size={17} className="mt-0.5 shrink-0 text-state-answerable" />
              ) : (
                <Circle size={17} className="mt-0.5 shrink-0 text-text-faint" />
              )}
              <div className="min-w-0 flex-1">
                <p className={cn(
                  "text-sm font-medium",
                  s.status === "done" && "text-text-muted",
                )}>
                  {s.title}
                </p>
                <p className="text-xs text-text-muted">{s.body}</p>
                {s.fix && <div className="mt-1.5">{s.fix}</div>}
              </div>
              {s.key === "sync" && allReady && (
                <Button
                  variant="primary"
                  onClick={() => void runFirstSync()}
                  disabled={syncing}
                  className="mt-0.5 shrink-0"
                >
                  {syncing ? "syncing…" : "Sync now"}
                </Button>
              )}
            </div>
          ))}
        </div>
      </Card>

      {allReady && (
        <Card className="border-dashed p-8 text-center">
          <p className="text-sm font-medium">Indexed. Try it:</p>
          <a
            href={`/chat?q=${encodeURIComponent(SAMPLE_QUESTION)}`}
            className="mono mt-3 inline-flex items-center gap-2 rounded-[--radius-sm] border border-accent/40 bg-accent-dim px-4 py-2 text-sm text-accent transition-colors hover:bg-accent/20"
          >
            {SAMPLE_QUESTION} <ArrowRight size={14} />
          </a>
          <p className="mono mt-3 text-[10px] text-text-faint">
            or type your own - every answer cites the exact span it used
          </p>
        </Card>
      )}

      <p className="mono px-1 text-[10px] text-text-faint">
        files stay on your Drive · Fenris reads (never writes) · the index lives only on this machine
      </p>
    </div>
  );
}

import { AlertTriangle, Wrench } from "lucide-react";
import { SetupNeeded } from "@/lib/api";

/**
 * The friendly 503: the engine is telling the UI what setup step is
 * missing. Rendered as a quiet banner, never a blocking modal.
 */
export function SetupNeededBanner({ error }: { error: SetupNeeded }) {
  const hint = error.reason.includes("Gemini")
    ? "Set GOOGLE_API_KEY on the machine running the engine, then restart it."
    : error.reason.includes("Drive") || error.reason.includes("OAuth") || error.reason.includes("keyring")
      ? "Run `fenris drive auth` once to connect your Drive."
      : "Start the engine with `fenris api` (or `make api`), then reload.";

  return (
    <div className="flex items-start gap-3 rounded-[--radius] border border-state-stale/30 bg-state-stale/5 p-4">
      <Wrench className="mt-0.5 shrink-0 text-state-stale" size={18} />
      <div className="text-sm">
        <p className="font-medium">Setup needed</p>
        <p className="text-text-muted">{hint}</p>
        <p className="mono mt-2 text-xs text-text-faint">{error.reason}</p>
      </div>
    </div>
  );
}

/** Inline error for failed loads — compact, with the raw reason preserved. */
export function LoadError({ error }: { error: Error }) {
  return (
    <div className="flex items-center gap-2 rounded-[--radius] border border-state-error/30 bg-state-error/5 p-3 text-sm text-text-muted">
      <AlertTriangle size={16} className="shrink-0 text-state-error" />
      <span className="truncate">{error.message}</span>
    </div>
  );
}

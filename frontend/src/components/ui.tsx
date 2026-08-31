import { cn } from "@/lib/utils";

/**
 * Local, dependency-light primitives in the shadcn idiom (cva-style
 * variants via cn), matching the Fenris palette. Only what the app
 * actually uses — no component-library import cost.
 */

export function Card({ className, children }: { className?: string; children: React.ReactNode }) {
  return (
    <div className={cn("rounded-[--radius] border border-border-subtle bg-bg-raised", className)}>
      {children}
    </div>
  );
}

export function CardHeader({
  title,
  action,
}: {
  title: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between border-b border-border-subtle px-4 py-3">
      <h2 className="text-sm font-medium text-text">{title}</h2>
      {action}
    </div>
  );
}

export function Stat({
  label,
  value,
  tone = "default",
}: {
  label: string;
  value: React.ReactNode;
  tone?: "default" | "accent" | "warn" | "danger";
}) {
  const toneClass = {
    default: "text-text",
    accent: "text-accent",
    warn: "text-state-stale",
    danger: "text-state-error",
  }[tone];
  return (
    <div className="flex flex-col gap-1 px-4 py-3">
      <span className="mono text-[10px] uppercase tracking-wider text-text-faint">
        {label}
      </span>
      <span className={cn("mono text-xl font-semibold", toneClass)}>{value}</span>
    </div>
  );
}

export function Badge({
  children,
  tone = "default",
}: {
  children: React.ReactNode;
  tone?:
    | "default"
    | "accent"
    | "answerable"
    | "stale"
    | "conflicting"
    | "insufficient"
    | "error";
}) {
  const tones = {
    default: "border-border text-text-muted",
    accent: "border-accent/40 text-accent bg-accent-dim",
    answerable: "border-state-answerable/40 text-state-answerable bg-state-answerable/5",
    stale: "border-state-stale/40 text-state-stale bg-state-stale/5",
    conflicting: "border-state-conflicting/40 text-state-conflicting bg-state-conflicting/5",
    insufficient: "border-border text-text-muted bg-bg-overlay",
    error: "border-state-error/40 text-state-error bg-state-error/5",
  }[tone];
  return (
    <span
      className={cn(
        "mono inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-medium tracking-wide",
        tones
      )}
    >
      {children}
    </span>
  );
}

export function Button({
  children,
  onClick,
  variant = "default",
  disabled,
  className,
}: {
  children: React.ReactNode;
  onClick?: () => void;
  variant?: "primary" | "default" | "ghost";
  disabled?: boolean;
  className?: string;
}) {
  const variants = {
    primary: "bg-accent text-bg hover:bg-accent-hover font-medium",
    default: "border border-border text-text hover:bg-bg-overlay",
    ghost: "text-text-muted hover:text-text hover:bg-bg-raised",
  }[variant];
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={cn(
        "rounded-[--radius-sm] px-3 py-1.5 text-sm transition-colors",
        "disabled:cursor-not-allowed disabled:opacity-50",
        variants,
        className
      )}
    >
      {children}
    </button>
  );
}

export function SkeletonRow({ className }: { className?: string }) {
  return (
    <div className={cn("h-4 animate-pulse rounded bg-bg-overlay", className)} />
  );
}

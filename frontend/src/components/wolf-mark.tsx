/**
 * Fenris wolf mark: geometric, minimal, howling-wolf silhouette.
 * No external files — this scales to any size.
 */

export function WolfMark({
  size = 28,
  className = "",
  glow = false,
}: {
  size?: number;
  className?: string;
  glow?: boolean;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
      aria-label="Fenris"
      style={glow ? { filter: "drop-shadow(0 0 6px rgba(122,162,247,0.45))" } : undefined}
    >
      {/* rounded-square badge */}
      <rect x="2" y="2" width="60" height="60" rx="14" fill="#101012" stroke="#2a2a30" strokeWidth="2" />

      {/* head + muzzle raised to the sky (howling) */}
      <path
        d="M20 50
           C20 50 22 34 30 30
           C30 30 30 22 34 15
           L36 21
           C36 21 42 24 44 30
           C48 38 46 50 46 50
           Z"
        fill="#7aa2f7"
      />

      {/* ear notch */}
      <path d="M33 17 L34.5 12.5 L37 20 Z" fill="#7aa2f7" />

      {/* eye: a single cut in the silhouette */}
      <circle cx="37" cy="30" r="2.2" fill="#0a0a0b" />

      {/* chin / jaw separation */}
      <path d="M36 44 C38 46 41 47 44 46" stroke="#0a0a0b" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  );
}

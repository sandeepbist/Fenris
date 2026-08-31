import { Card, CardHeader, SkeletonRow, Stat } from "@/components/ui";

export function DashboardPage() {
  return (
    <div className="mx-auto max-w-3xl p-6">
      <h1 className="mb-4 text-lg font-medium">Dashboard</h1>
      <Card>
        <CardHeader title="Index" />
        <div className="grid grid-cols-3 divide-x divide-border-subtle">
          <Stat label="active sources" value={<SkeletonRow className="w-16" />} />
          <Stat label="chunks" value={<SkeletonRow className="w-16" />} />
          <Stat label="drive files" value={<SkeletonRow className="w-16" />} />
        </div>
      </Card>
      <p className="mono mt-4 text-xs text-text-faint">
        dashboard slice lands next
      </p>
    </div>
  );
}

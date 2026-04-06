import { Skeleton } from "@/components/ui/skeleton"
import { TableCell, TableRow } from "@/components/ui/table"

export function TableLoading({
  columns,
  rows = 5,
}: {
  columns: number
  rows?: number
}) {
  return (
    <>
      {Array.from({ length: rows }, (_, i) => (
        <TableRow key={i}>
          <TableCell colSpan={columns}>
            <Skeleton className="h-5 w-full" />
          </TableCell>
        </TableRow>
      ))}
    </>
  )
}

export function TableMessage({
  columns,
  children,
  tone = "muted",
}: React.PropsWithChildren<{ columns: number; tone?: "muted" | "danger" }>) {
  return (
    <TableRow>
      <TableCell
        colSpan={columns}
        className={
          tone === "danger"
            ? "py-10 text-center text-sm text-destructive"
            : "py-10 text-center text-sm text-muted-foreground"
        }
      >
        {children}
      </TableCell>
    </TableRow>
  )
}

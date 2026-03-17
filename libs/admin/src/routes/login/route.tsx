import { Button } from "@/components/ui/button"
import { useAuthStore } from "@/modules/auth/store"
import { IconTopologyStar3 } from "@tabler/icons-react"
import { createFileRoute, useNavigate } from "@tanstack/react-router"

export const Route = createFileRoute("/login")({
  component: LoginPage,
})

function LoginPage() {
  const signIn = useAuthStore((s) => s.signIn)
  const navigate = useNavigate()

  const handleSignIn = () => {
    signIn()
    navigate({ to: "/clusters" })
  }

  return (
    <div className="flex min-h-svh items-center justify-center bg-background p-6">
      <div className="w-full max-w-sm space-y-6 rounded-xl border bg-card p-8">
        <div className="flex items-center gap-3">
          <div className="flex size-9 items-center justify-center rounded-md bg-primary text-primary-foreground">
            <IconTopologyStar3 className="size-5" />
          </div>
          <div>
            <p className="text-base font-semibold leading-none">Hangar</p>
            <p className="mt-1 font-mono text-2xs uppercase tracking-[0.18em] text-muted-foreground/60">
              Control plane console
            </p>
          </div>
        </div>
        <p className="text-sm text-muted-foreground">
          Authentication is stubbed. Continuing issues a static development
          token.
        </p>
        <Button className="w-full" onClick={handleSignIn}>
          Continue
        </Button>
      </div>
    </div>
  )
}

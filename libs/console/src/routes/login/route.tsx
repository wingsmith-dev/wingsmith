import { Button } from "@/components/ui/button"
import { useAuthStore } from "@/modules/auth/store"
import { HangarMark } from "@/components/layout/hangar-mark"
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
        <div className="flex items-center justify-center gap-3">
          <div className="flex size-9 items-center justify-center rounded-md bg-white text-[#0b1220]">
            <HangarMark className="size-5" />
          </div>
          <p className="text-base leading-none font-semibold tracking-[0.14em] uppercase">
            HANGAR
          </p>
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

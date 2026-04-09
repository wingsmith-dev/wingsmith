import { useState } from "react"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
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

  const [password, setPassword] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault()
    setPending(true)
    setError(null)
    try {
      await signIn(password)
      navigate({ to: "/clusters" })
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Sign in failed.")
      setPassword("")
    } finally {
      setPending(false)
    }
  }

  return (
    <div className="flex min-h-svh items-center justify-center bg-background p-6">
      <form
        onSubmit={handleSubmit}
        className="w-full max-w-sm space-y-6 rounded-xl border bg-card p-8"
      >
        <div className="flex items-center justify-center gap-3">
          <div className="flex size-9 items-center justify-center rounded-md bg-white text-[#0b1220]">
            <HangarMark className="size-5" />
          </div>
          <p className="text-base leading-none font-semibold tracking-[0.14em] uppercase">
            HANGAR
          </p>
        </div>

        <div className="space-y-2">
          <Label htmlFor="password">Admin password</Label>
          <Input
            id="password"
            type="password"
            autoFocus
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            disabled={pending}
          />
        </div>

        {error ? (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        ) : null}

        <Button
          className="w-full"
          type="submit"
          disabled={pending || !password}
        >
          {pending ? "Signing in…" : "Sign in"}
        </Button>
      </form>
    </div>
  )
}

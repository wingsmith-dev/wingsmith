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

/**
 * One form for both audiences, told apart by the name typed into it.
 *
 * `admin` is the fleet operator and the credential is the shared password. Any
 * other name is a tenant and the credential is their token. Two forms would mean
 * a second URL to publish and a wrong-door state to design; one field costs the
 * operator four keystrokes they can leave in place.
 *
 * Only the tenant credential is trimmed. A token is `hgrt1.` and 32 url-safe
 * random bytes, so it can never legitimately carry surrounding whitespace and a
 * pasted one that does is a paste artefact — which otherwise fails the hash
 * lookup and reports itself as a wrong token. The admin password is not ours to
 * assume that about and is sent exactly as typed.
 */
function LoginPage() {
  const signIn = useAuthStore((s) => s.signIn)
  const navigate = useNavigate()

  const [slug, setSlug] = useState("admin")
  const [password, setPassword] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)

  const tenant = slug.trim() !== "" && slug.trim() !== "admin"

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault()
    setPending(true)
    setError(null)
    try {
      await signIn(tenant ? password.trim() : password, slug.trim() || "admin")
      navigate({ to: tenant ? "/serving" : "/clusters" })
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
          <Label htmlFor="slug">Account</Label>
          <Input
            id="slug"
            autoFocus
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            autoComplete="username"
            value={slug}
            onChange={(e) => setSlug(e.target.value)}
            disabled={pending}
          />
        </div>

        <div className="space-y-2">
          <Label htmlFor="password">
            {tenant ? "Token" : "Admin password"}
          </Label>
          <Input
            id="password"
            type="password"
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

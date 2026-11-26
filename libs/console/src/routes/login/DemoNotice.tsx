/** Public-demo hint on the sign-in page. Renders nothing unless a demo password is configured. */
export function DemoNotice({ password }: { password: string }) {
  if (!password) return null
  return (
    <p role="note" className="rounded-md bg-muted p-3 text-sm">
      Public demo, read-only. Sign in with password:{" "}
      <code className="font-mono font-semibold">{password}</code>
    </p>
  )
}

import { MockNotice } from "@/components/workbench/mock-notice"
import { StatRow, StatTile } from "@/components/layout/stat-tile"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { StatusLabel } from "@/components/layout/status-dot"
import { cn } from "@/lib/utils"
import { formatMs } from "@/modules/benchmarks/display"
import {
  mockSessions,
  mockTranscript,
  type AgentSession,
  type AgentTurn,
} from "@/modules/benchmarks/mock"
import { useMemo, useState } from "react"

/**
 * Whether a real client can drive the model through a whole task.
 *
 * The claim is not shape and not accuracy: it is that an ordinary agent loop —
 * many turns, tools called and answered, a long context that grows — completes.
 * It catches what neither of the other two kinds does, because both of those
 * ask one request at a time and the failures here are failures of the sequence:
 * a stream truncated inside tool-call arguments, `tool_choice=required` quietly
 * answered with prose, the same edit attempted four times.
 *
 * So the headline is a completion count and never a latency. A session that
 * finished in ninety seconds and a session that finished in nine both count
 * once, and a fast failure counts for nothing.
 */
export function AgentSuite({ id }: { id: string }) {
  const sessions = useMemo(() => mockSessions(id), [id])
  const completed = sessions.filter((session) => session.completed).length
  const turns = sessions.reduce((sum, session) => sum + session.turns, 0)
  const toolCalls = sessions.reduce((sum, s) => sum + s.toolCalls, 0)
  const retries = sessions.reduce((sum, s) => sum + s.retries, 0)

  return (
    <div className="space-y-4">
      <MockNotice what="Agent sessions, invented — no `agentic` harness is registered." />

      <StatRow>
        <StatTile
          label="Completed"
          value={`${completed} / ${sessions.length}`}
          tone={completed === sessions.length ? "ok" : "danger"}
        />
        <StatTile label="Turns" value={String(turns)} />
        <StatTile label="Tool calls" value={String(toolCalls)} />
        <StatTile
          label="Retries"
          value={String(retries)}
          tone={retries > 0 ? "warn" : undefined}
        />
        <StatTile
          label="Clients"
          value={String(new Set(sessions.map((s) => s.client)).size)}
          size="sm"
        />
      </StatRow>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">Sessions</CardTitle>
          <p className="mt-1 text-2xs text-muted-foreground">
            One row per client and task. A failure names the first thing that
            went wrong, verbatim — a session that ended for an unnamed reason is
            not evidence of anything.
          </p>
        </CardHeader>
        <CardContent className="px-0">
          <div className="divide-y divide-border/60 border-y">
            {sessions.map((session) => (
              <SessionLine key={session.id} session={session} />
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}

function SessionLine({ session }: { session: AgentSession }) {
  return (
    <div className="flex flex-wrap items-center gap-2 px-3 py-2 hover:bg-muted/40">
      <StatusLabel tone={session.completed ? "ok" : "danger"}>
        {session.completed ? "completed" : "broke"}
      </StatusLabel>
      <Badge variant="outline" className="font-mono text-2xs">
        {session.client}
      </Badge>
      <span className="min-w-0 flex-1 truncate text-xs">{session.task}</span>
      <span className="font-mono text-2xs text-muted-foreground">
        {session.turns} turns
      </span>
      <span className="hidden font-mono text-2xs text-muted-foreground sm:inline">
        {session.toolCalls} calls
      </span>
      {session.retries > 0 && (
        <span className="font-mono text-2xs text-warning">
          {session.retries} retries
        </span>
      )}
      <span className="w-16 text-right font-mono text-2xs text-muted-foreground">
        {formatMs(session.ms)}
      </span>
      {session.failure && (
        <p className="w-full pl-1 font-mono text-2xs text-red-600 dark:text-red-400">
          {session.failure}
        </p>
      )}
    </div>
  )
}

/**
 * The run view: one session, turn by turn.
 *
 * The transcript is the evidence, the same way the raw exchange is the evidence
 * under a conformance row. A verdict that says "the loop broke" and makes you
 * leave the page to find out where is the thing this replaces.
 */
export function AgentRun({ id }: { id: string }) {
  const turns = useMemo(() => mockTranscript(id), [id])
  const sessions = useMemo(() => mockSessions(id, 1), [id])
  const session = sessions[0]

  return (
    <div className="space-y-4">
      <MockNotice what="One agent session's transcript, invented." />

      <StatRow>
        <StatTile
          label="Outcome"
          value={session.completed ? "completed" : "broke"}
          size="sm"
          tone={session.completed ? "ok" : "danger"}
        />
        <StatTile label="Client" value={session.client} size="sm" />
        <StatTile label="Turns" value={String(turns.length)} />
        <StatTile
          label="Tool calls"
          value={String(turns.filter((turn) => turn.tool).length)}
        />
        <StatTile
          label="Wall clock"
          value={formatMs(turns.reduce((sum, turn) => sum + turn.ms, 0))}
          size="sm"
        />
      </StatRow>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">Transcript</CardTitle>
        </CardHeader>
        <CardContent className="space-y-1.5">
          {turns.map((turn) => (
            <TurnLine key={turn.index} turn={turn} />
          ))}
        </CardContent>
      </Card>
    </div>
  )
}

const ROLE_STYLE: Record<AgentTurn["role"], string> = {
  user: "border-l-muted-foreground/30",
  assistant: "border-l-primary/50",
  tool: "border-l-amber-500/50",
}

function TurnLine({ turn }: { turn: AgentTurn }) {
  const [open, setOpen] = useState(false)
  const long = turn.text.length > 90
  return (
    <button
      type="button"
      onClick={() => long && setOpen(!open)}
      className={cn(
        "flex w-full gap-2 border-l-2 py-1 pl-3 text-left",
        ROLE_STYLE[turn.role],
        long && "hover:bg-muted/40"
      )}
    >
      <span className="w-16 shrink-0 font-mono text-2xs text-muted-foreground">
        {turn.role}
      </span>
      {turn.tool && (
        <Badge variant="outline" className="h-4 shrink-0 font-mono text-2xs">
          {turn.tool}
        </Badge>
      )}
      <span
        className={cn(
          "min-w-0 flex-1 font-mono text-2xs whitespace-pre-wrap",
          !open && "truncate whitespace-nowrap"
        )}
      >
        {turn.text}
      </span>
      <span className="w-12 shrink-0 text-right font-mono text-2xs text-muted-foreground">
        {formatMs(turn.ms)}
      </span>
    </button>
  )
}

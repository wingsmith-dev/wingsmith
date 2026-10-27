import ts from "typescript"
import { describe, expect, it } from "vitest"

/**
 * The console's wire types are hand-written, so this checks them against the
 * plane's committed OpenAPI document: every field a console interface declares
 * must exist on the schema it says it mirrors. A field the plane never sends (or
 * never accepts) fails here instead of rendering as a permanent blank.
 *
 * An interface is matched by the schema its doc comment names first
 * (`` `ClusterSchema` — ...``), or by its own name when the document defines it.
 * Console types may omit fields; they may not invent them.
 *
 * A response field the console types as required and non-null must not be
 * nullable in the schema either. That is what lets a page read
 * `workload.labels` without a `?? {}` guard: the type is the plane's promise,
 * and this keeps it one.
 */
type Property = {
  type?: string | string[]
  anyOf?: Property[]
  oneOf?: Property[]
}

const documents = import.meta.glob<{
  components: {
    schemas: Record<string, { properties?: Record<string, Property> }>
  }
}>("../../packages/contract/openapi.json", { eager: true, import: "default" })

const sources = import.meta.glob<string>("./modules/*/types.ts", {
  query: "?raw",
  import: "default",
  eager: true,
})

/** Schemas the document qualifies because two plane modules share the name. */
const QUALIFIED: Record<string, string> = {
  "binding/Policy": "policies_schemas_PolicySchema",
  "demand/EndpointPolicy": "endpoints_schemas_PolicySchema",
}

/**
 * The schema a doc comment opens with. A name ending in Schema or Request is taken
 * as a claim even when the document lacks it, so a stale one fails; any other
 * leading identifier counts only if the document defines it.
 */
function docName(
  node: ts.InterfaceDeclaration,
  schemas: Record<string, unknown>
): string | null {
  const docs = (node as unknown as { jsDoc?: ts.JSDoc[] }).jsDoc ?? []
  const text = docs.map((doc) => ts.getTextOfJSDocComment(doc.comment) ?? "")
  const name = text.join("\n").match(/^`([A-Za-z_]+)`/)?.[1]
  if (!name) return null
  return /(Schema|Request)$/.test(name) || name in schemas ? name : null
}

const document = Object.values(documents)[0]

function nullable(property: Property): boolean {
  const type = property.type
  if (type === "null" || (Array.isArray(type) && type.includes("null")))
    return true
  return (property.anyOf ?? property.oneOf ?? []).some(nullable)
}

describe.skipIf(!document)("console types against openapi.json", () => {
  it("declares no field the plane's schema lacks", () => {
    const schemas = document.components.schemas
    const problems: string[] = []
    let checked = 0

    for (const [file, text] of Object.entries(sources)) {
      const module = file.split("/")[2]
      const source = ts.createSourceFile(
        file,
        text,
        ts.ScriptTarget.Latest,
        true
      )
      for (const node of source.statements) {
        if (!ts.isInterfaceDeclaration(node)) continue
        const own = node.name.text
        const named =
          QUALIFIED[`${module}/${own}`] ??
          docName(node, schemas) ??
          (own in schemas ? own : null)
        if (!named) continue
        const schema = schemas[named]
        if (!schema) {
          problems.push(`${module}/${own} names ${named}, which is not defined`)
          continue
        }
        if (!schema.properties) continue
        checked++
        for (const member of node.members) {
          if (!member.name) continue
          const field = member.name.getText(source).replace(/"/g, "")
          const property = schema.properties[field]
          if (!(field in schema.properties)) {
            problems.push(`${module}/${own}.${field} is not in ${named}`)
            continue
          }
          if (
            ts.isPropertySignature(member) &&
            !member.questionToken &&
            !/Request$/.test(named) &&
            !/\bnull\b/.test(member.type?.getText(source) ?? "") &&
            nullable(property)
          )
            problems.push(
              `${module}/${own}.${field} is typed non-null, but ${named} allows null`
            )
        }
      }
    }

    expect(checked).toBeGreaterThan(50)
    expect(problems).toEqual([])
  })
})

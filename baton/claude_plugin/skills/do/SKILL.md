---
name: do
description: Requirements → PRD → issues. Grills the idea, writes a PRD as a GitHub issue, breaks it into child issues, then clears context. Hand off to /baton:implement to execute.
---

# /baton:do — Requirements to Issues

Orchestrate the first half of the feature lifecycle: grilling → PRD → issue breakdown → context reset. Follow each phase in order without skipping.

## Arguments

`$ARGUMENTS` may contain `@file` references. If any `@` references are present, use `/baton:grill-with-docs`; otherwise use `/baton:grilling`.

---

## Phase 1 — Requirements Grilling

- If `$ARGUMENTS` contains any `@` file references → invoke `/baton:grill-with-docs $ARGUMENTS`
- Otherwise → invoke `/baton:grilling`

Run the full grilling session until you have a clear, stable picture of what is being built.

---

## Phase 2 — PRD (automatic, no confirmation)

Invoke `/baton:to-prd`.

**Override the default `/baton:to-prd` behaviour**: do NOT pause to ask the user whether the seams look correct. Proceed directly through all steps and publish the PRD as a GitHub issue using `gh issue create`. Apply both the `ready-for-agent` and `prd` triage labels.

---

## Phase 3 — Issue Breakdown (automatic, no confirmation)

Invoke `/baton:to-issues`.

**Override the default `/baton:to-issues` behaviour**: do NOT run the "Quiz the user" step. Do not ask whether granularity or dependencies look right. Proceed directly to publishing all slices as individual GitHub issues via `gh issue create`, in dependency order (blockers first).

---

## Phase 4 — Context Reset

Run `/clear`.

---

**Done.** Use `/baton:implement` to pick a PRD and execute the implementation, testing, QA, and close phases.

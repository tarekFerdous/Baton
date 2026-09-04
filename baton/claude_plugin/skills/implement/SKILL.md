---
name: implement
description: Pick a PRD from open GitHub issues, implement its child issues one at a time in this session, run tests, write a tracker file, then clear. Hand off to /baton:qa to finish.
---

# /baton:implement — Implementation Workflow

Execute implementation and testing for a chosen PRD. Follow each phase in order without skipping.

---

## Phase 1 — PRD Selection

If the invocation already specifies `prd: <N>` (e.g. `/baton:implement prd: 34`), skip this entire phase — no `gh issue list` fetch, no table, no pause — and go straight to Phase 2 using PRD issue N as the selected PRD. Otherwise, continue with steps 1-6 below.

1. Fetch all open issues:
   ```
   gh issue list --state open --json number,title,body,labels
   ```
2. Identify **PRD issues** — those with the `ready-for-agent` label or whose title/body marks them as a PRD.
3. For each PRD, collect its related child issues (issues whose body references the PRD number, e.g. "Part of #N" or "Blocked by #N").
4. Display a table like the following (one row per PRD; child issue titles in the last column):

   | # | PRD Title | Child Issues |
   |---|-----------|--------------|
   | 12 | User auth flow | #13 Sign-up page, #14 Login page, #15 JWT middleware |
   | 20 | Menu management | #21 Category CRUD, #22 Item CRUD, #23 Image upload |

5. **Pause here.** Ask the user: "Which PRD would you like to implement? (enter the issue number)"
6. Wait for the user's selection before continuing.

---

## Phase 2 — Implementation

Using only the child issues that belong to the selected PRD:

1. Re-fetch those issues to get their current bodies and labels.
2. Identify **unblocked** issues (those whose body says "None - can start immediately" or whose blockers are already closed).
3. Implement all unblocked issues **one at a time, in this same continuous session** — do not dispatch sub-agents and do not implement issues in parallel, regardless of how many are unblocked at once. Baton-sized PRDs are small enough that sequential is cheap; parallel sub-agent fan-out is what was burning the account's rate-limit budget.
4. Once unblocked issues are complete, resolve the next wave of now-unblocked issues — also one at a time.
5. **Triage is human-only.** If any issue requires a triage decision, pause and ask the user before continuing.

---

## Phase 3 — Testing

Run the project's full test suite. All tests must pass before continuing. Fix any failures.

---

## Phase 4 — Write Tracker File

Write `.claude/implement-tracker.json` at the project root with the following structure:

```json
{
  "prd": {
    "number": <prd-issue-number>,
    "title": "<prd-issue-title>"
  },
  "issues": [
    {
      "number": <issue-number>,
      "title": "<issue-title>",
      "summary": "<one or two sentences describing what was built for this issue>",
      "acceptance_criteria": ["<criterion 1>", "<criterion 2>"]
    }
  ],
  "qa_changes": [],
  "status": "implemented"
}
```

- `qa_changes` starts as an empty array — `/baton:qa` will populate it during the QA loop.
- `acceptance_criteria` lists every criterion from the issue body so `/baton:qa` can check them off.

---

## Phase 5 — Context Reset

Run `/clear`.

---

**Done.** Use `/baton:qa` to run the QA loop, close issues, commit, and clean up.

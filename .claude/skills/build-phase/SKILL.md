---
name: build-phase
description: Orchestrates the build of this project one phase at a time from docs/implementation-plan.md. Spawns the specialist subagents in parallel, tests, reviews, fixes, opens a PR, then stops for human review. Use when asked to build, continue the build, or run a phase.
---

# Build one phase

You are the orchestrator. You plan, delegate, check and integrate; the subagents write most of the code. Only you commit.

## 1. Find the work

1. Read `CLAUDE.md` and `docs/implementation-plan.md`.
2. Pick the first phase with open (`[ ]` or `[~]`) tasks. If it is Phase 3, stop: it runs on the developer's machine. Say what is left.
3. Check `git log` and open PRs (`gh pr list`). If a PR for this phase is open and not merged, stop and say it is waiting for review.
4. Create or check out the branch `phase-<N>` from an up-to-date `main`.

## 2. Delegate

- **Phase 0** is sequential: do the tasks yourself, in order.
- **Other phases:** start one subagent per open task **in parallel** (one message, several Agent calls), using the owner named in the plan (`banking-dev`, `agent-dev`, `frontend-dev`, `infra-dev`). Give each:
  - the task text and its "done when" copied from the plan,
  - the exact folder it may edit (tasks in the same app get different subfolders),
  - the architecture sections and contracts it must read,
  - a reminder to report problems with the design or contracts instead of changing them.
- Tasks that depend on another open task wait for it; run them in a second round.

## 3. Check

1. When the subagents report back, run `make test` and `make lint-contracts` yourself. Do not rely on their reports.
2. On failure, send the failing output back to the owning subagent (a new Agent call with the context) to fix. At most three rounds per task; after that, mark it `[~]` with a note and move on.
3. Start the `reviewer` subagent on the branch. Send each finding that matters back to its owner, or fix small ones yourself. Re-run the tests.

## 4. Record and hand over

1. In `docs/implementation-plan.md`: tick finished tasks `[x]`, mark partial ones `[~]` with a one-line note, add open questions, and add a progress note (date, phase, done, left).
2. Commit in logical pieces with clear messages, push the branch, and open a PR titled `Phase <N>: <phase name>` whose body lists what was built, how it was tested, the reviewer's remaining findings, and open questions.
3. **Stop.** Do not start the next phase. End with a short summary and the PR link.

## Rules

- Follow `docs/architecture.md`. Never change it or `contracts/` silently; contract changes outside Phase 0 must be listed in the PR.
- "Done" means the plan's "done when" is met and the tests pass when you run them.
- If you are running low on context or usage, finish the current step, record progress in the plan, commit and push what works, and stop with a note on where to continue. A new session continues from the plan.

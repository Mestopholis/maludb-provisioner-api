# Execution Plans

Use an execution plan for work that spans multiple components, contains meaningful architectural risk, or is unlikely to be completed safely as one small change.

## Location

- Active: `plans/active/<descriptive-name>.md`
- Completed: `plans/completed/<descriptive-name>.md`
- Template: `plans/templates/EXECUTION-PLAN.md`

## Required plan fields

Every active plan must contain:

- Status
- Human owner
- Agent/tool being used
- Branch
- Related task/phase
- Dependencies
- Scope
- Non-goals
- Implementation steps
- Test/verification steps
- Risks
- Decision log
- Progress log

## Rules

- A plan is a living document.
- Keep the plan synchronized with reality.
- If scope changes, update the plan before broadening implementation.
- Do not use a plan to override `docs/DECISIONS.md`.
- Move completed plans to `plans/completed/`.
- Multiple developers may work concurrently, but they should own different plans/branches unless intentionally pairing.

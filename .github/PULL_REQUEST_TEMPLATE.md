## What does this PR do?

<!-- One paragraph. What changed, why, and the evidence (tests, measurements). -->

## Checklist

- [ ] `make test` green; `make lint` green (CI runs both on the PR)
- [ ] New behavior has tests in the same commit; bug fixes carry a regression test that fails without the fix
- [ ] No correctness invariant weakened (see CONTRIBUTING.md) — if any line of the PR trades honesty for fluency, say so explicitly in the description
- [ ] Commit messages follow the conventional format with scope
- [ ] No secrets, tokens, or personal file paths in the diff
- [ ] All network behavior stays offline-testable (fakes, not live calls)

## Evidence

<!-- Test counts, eval numbers, before/after. Hand-computed expected values for anything mathematical. -->

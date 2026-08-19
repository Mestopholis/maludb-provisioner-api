#!/bin/bash
# The security review, made a merge gate rather than a checklist item.
#
# Phase 07's plan asked for a security review before merge on every slice. Four
# slices merged without one, and the catch-up pass found three issues in shipped
# code -- including a customer able to grant themselves `direct_database_access`.
# Phase 08's plan asked again, in stronger words, and named slice 1 in advance as
# not mergeable on a green suite alone. Slice 1 is the one slice of eleven that
# merged with no review recorded, and the catch-up pass found ADR-046 in it.
#
# Twice is a pattern, and the pattern is not forgetfulness: the control was a
# line of prose in a plan file, checked by whoever was also doing the work. This
# script moves it to where it cannot be skipped by not thinking about it.
#
# What it can and cannot do. CI cannot judge a review; nothing here reads code.
# What it enforces is that the review left evidence, in the one place that
# survives a branch deletion and can be audited years later -- the commit
# message. That is not a formality: finding the Phase 08 gap at all meant
# grepping commit bodies and a progress log, and the two slices that *had* been
# reviewed were legible precisely because they said so there.
#
# A change declares its review with a trailer on at least one of its commits:
#
#   Security-Review: none
#   Security-Review: 2 findings, both fixed -- unsanitised name reaching a
#                    terminal, truncated snapshot compared as complete
#
# `none` is an honest and common answer. What is not available is silence.
#
# What it defends against is forgetting, not an adversary. A branch can edit
# this script, and a required check whose logic lives in the branch it gates is
# advisory against anybody with commit access -- which is true of every check in
# this workflow. Malice is branch protection's problem: required reviewers, and
# a protected `main`. Twice now the thing that actually happened was neither
# malice nor a decision, but a control nobody could see the absence of.
#
# Docs-only changes are exempt; everything else is gated, tests included. A
# tests-only change is not customer-reachable, and a diff that deletes a
# negative assertion is exactly how a control disappears -- so the cheaper
# mistake is to ask for a line that says "none".
#
#   scripts/require-security-review.sh <base-ref> <head-ref>
#
# Run by .github/workflows/ci.yml on every pull request, and locally before
# opening one.

set -euo pipefail

BASE=${1:?usage: require-security-review.sh <base-ref> <head-ref>}
HEAD=${2:?usage: require-security-review.sh <base-ref> <head-ref>}

TRAILER='Security-Review:'

# Paths that cannot change behaviour a customer, a tenant or an operator meets.
# Deliberately short: anything not named here is gated. A new top-level
# directory is gated by default rather than exempt by default, which is the
# safer way round for a list somebody will forget to update.
EXEMPT_PREFIXES='docs/|plans/|tasks/'
EXEMPT_FILES='README.md|AGENTS.md|CLAUDE.md|PLANS.md'

changed=$(git diff --name-only "$BASE".."$HEAD")

if [ -z "$changed" ]; then
  echo "no files changed between $BASE and $HEAD; nothing to review"
  exit 0
fi

gated=$(echo "$changed" | grep -vE "^($EXEMPT_PREFIXES)" | grep -vE "^($EXEMPT_FILES)\$" || true)

if [ -z "$gated" ]; then
  echo "documentation only -- no security review required:"
  echo "$changed" | sed 's/^/  /'
  exit 0
fi

echo "changes requiring a recorded security review:"
echo "$gated" | sed 's/^/  /'
echo

# Every commit the pull request adds, not just its tip: a review recorded on
# the commit that answered it is more useful later than one squashed onto the
# end, and the branch may legitimately carry several.
# Unindented, which is what a git trailer is -- `git interpret-trailers` reads
# the last paragraph and nothing indented under it. Written leniently first,
# and the first run caught its own commit: a message that *explains* the
# convention quotes the format, and an indented example satisfied the gate. A
# commit describing a security review is not one.
declared=$(git log --format=%B "$BASE".."$HEAD" | grep -iE "^$TRAILER" || true)

if [ -z "$declared" ]; then
  echo "::error::no '$TRAILER' trailer on any commit in this change"
  cat <<'MSG'

A security review before merge is required by AGENTS.md, and this change
touches something other than documentation. Record the outcome on a commit:

    Security-Review: none

or, where it found something:

    Security-Review: 1 finding, fixed -- <what it was>

`none` is a real answer and a common one. The trailer is what makes the review
auditable after the branch is deleted; two phases of this repository have a
security finding that shipped because the review was a checklist item nobody
could see the absence of.
MSG
  exit 1
fi

# A trailer that says nothing is worse than an absent one: it reads as a review
# in every later audit. These are the values that mean "I have not done this
# yet" -- rejected by name so that writing one is a deliberate act rather than
# a habit.
placeholder=$(echo "$declared" \
  | sed -E "s/^$TRAILER[[:space:]]*//I" \
  | grep -icE '^(|-|n/?a|tbd|todo|pending|later|\?+)$' || true)

if [ "$placeholder" -gt 0 ]; then
  echo "::error::the '$TRAILER' trailer is a placeholder rather than an answer"
  echo "  write 'none' if the review found nothing; that is an answer and this is not."
  exit 1
fi

echo "security review recorded:"
echo "$declared" | sed 's/^[[:space:]]*/  /'

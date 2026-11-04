#!/usr/bin/env bash
# Open, update or close the single "Data updater is failing" issue.
#
#   failure-issue.sh open <failed step>   after a failed run
#   failure-issue.sh close                after a successful run
#
# Needs GH_TOKEN, GITHUB_REPOSITORY and RUN_URL. Reads update.log when present.
set -euo pipefail

label="updater-failure"
title="Data updater is failing"
mode="${1:?usage: failure-issue.sh open <step> | close}"

open_issue() {
  local number
  number="$(gh issue list --repo "$GITHUB_REPOSITORY" --label "$label" --state open \
    --json number --jq '.[0].number // empty' 2>/dev/null || true)"
  if [ -z "$number" ]; then
    # Fallback in case the label could not be created.
    number="$(gh issue list --repo "$GITHUB_REPOSITORY" --state open --search "\"$title\" in:title" \
      --json number,title --jq "map(select(.title == \"$title\")) | .[0].number // empty" 2>/dev/null || true)"
  fi
  echo "$number"
}

if [ "$mode" = "close" ]; then
  number="$(open_issue)"
  if [ -n "$number" ]; then
    gh issue close "$number" --repo "$GITHUB_REPOSITORY" \
      --comment "Resolved: [this run]($RUN_URL) succeeded and the data is updating again."
  fi
  exit 0
fi

step="${2:-an unknown step}"
error="(no updater output; the run failed before the data refresh)"
if [ -f update.log ]; then
  error="$(grep -E '^ERROR: ' update.log | tail -n 1 | cut -c 8- || true)"
  [ -n "$error" ] || error="$(tail -n 5 update.log)"
fi
fingerprint="$(printf '%s|%s' "$step" "$error" | sha256sum | cut -c 1-12)"

{
  echo "The scheduled AA-Omniscience update is failing. **The site keeps showing the last good data** until this is fixed. This issue closes itself after the next successful run."
  echo
  echo "**Latest failure:** $(date -u '+%Y-%m-%d %H:%M UTC'), [workflow run]($RUN_URL), failed at: **$step**"
  echo
  echo '```'
  echo "$error"
  echo '```'
  if [ -f update.log ]; then
    echo
    echo "<details><summary>Last 40 lines of the updater log</summary>"
    echo
    echo '```'
    tail -n 40 update.log
    echo '```'
    echo "</details>"
  fi
  echo
  echo "### What to do"
  echo "- **Sanity guard** in the error, and the change is genuine (for example AA re-scored every model)? Re-run *Update AA-Omniscience data* from the Actions tab with **Allow large changes** ticked."
  echo "- **Anything else:** download the \`aa-snapshot-…\` artifact from the run page (when present), then follow *When AA changes its site* in the README. Reproduce locally with \`python scripts/update_data.py --from-snapshot <folder> --dry-run\`."
  echo "- A one-off network failure needs no action; the next scheduled run retries."
  echo
  echo "<!-- fingerprint: $fingerprint -->"
} > issue-body.md

number="$(open_issue)"
if [ -n "$number" ]; then
  previous="$(gh issue view "$number" --repo "$GITHUB_REPOSITORY" --json body --jq .body \
    | grep -o 'fingerprint: [0-9a-f]*' | cut -d ' ' -f 2 || true)"
  gh issue edit "$number" --repo "$GITHUB_REPOSITORY" --body-file issue-body.md
  if [ "$previous" != "$fingerprint" ]; then
    gh issue comment "$number" --repo "$GITHUB_REPOSITORY" \
      --body "The failure changed ([run]($RUN_URL), step: $step). The issue description shows the latest error."
  fi
else
  gh label create "$label" --repo "$GITHUB_REPOSITORY" --color B60205 \
    --description "The scheduled data update is failing" 2>/dev/null || true
  gh issue create --repo "$GITHUB_REPOSITORY" --title "$title" --label "$label" --body-file issue-body.md \
    || gh issue create --repo "$GITHUB_REPOSITORY" --title "$title" --body-file issue-body.md
fi

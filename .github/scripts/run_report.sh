#!/usr/bin/env bash
# The result of every job of this workflow run as one table in the run's summary, and an error
# annotation when one of them is not green.  Used by the scheduled (nightly) runs of ci.yml and
# native-check.yml, so a red nightly is visible on the run page without opening each job.
# Nothing is posted anywhere (no issue, no comment, no message).
#
#   GH_TOKEN=... run_report.sh "<title>" "<name of the calling job>"
#
# Needs `actions: read` (the job list of this run) and the gh CLI of the runner image.
set -euo pipefail

title=${1:?title}
self=${2:?name of the calling job}
run_url="$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID"
jobs=$(mktemp)

gh api --paginate "repos/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID/attempts/$GITHUB_RUN_ATTEMPT/jobs" \
  --jq ".jobs[] | select(.name != \"$self\") | [.name, (.conclusion // .status)] | @tsv" > "$jobs"

bad=$(awk -F'\t' '$2 != "success" && $2 != "skipped"' "$jobs" | wc -l | tr -d ' ')
{
  if [ "$bad" -gt 0 ]; then
    echo "## $title: $bad job(s) not green"
  else
    echo "## $title: every job passed"
  fi
  echo
  echo "Commit \`$GITHUB_SHA\`, [this run]($run_url)."
  echo
  echo "| job | result |"
  echo "|---|---|"
  awk -F'\t' '{ r = ($2 == "success") ? "passed" : (($2 == "skipped") ? "skipped" : "**" toupper($2) "**"); print "| " $1 " | " r " |" }' "$jobs"
} >> "$GITHUB_STEP_SUMMARY"

if [ "$bad" -gt 0 ]; then
  echo "::error title=$title not green::$bad job(s) failed or were cancelled on $GITHUB_SHA ($run_url)"
  exit 1
fi

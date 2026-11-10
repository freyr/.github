#!/usr/bin/env bash
# Apply the freyr organization standard with gh (needs the admin:org scope).
#   org/apply.sh repos      repo merge settings + security settings, all repos
#   org/apply.sh org        organization Actions + security defaults
#   org/apply.sh rulesets   create or update the org rulesets (both start in evaluate mode)
#   org/apply.sh activate   set the rulesets "main" and "release-tags" to active
# Environment: ORG (default freyr), RELEASE_APP_ID (for the tag ruleset bypass).
set -euo pipefail
ORG=${ORG:-freyr}
here=$(cd "$(dirname "$0")" && pwd)

repos() {
  gh repo list "$ORG" --limit 500 --no-archived --json name,visibility \
    --jq '.[] | "\(.name) \(.visibility)"'
}

apply_repos() {
  repos | while read -r name visibility; do
    echo "== $ORG/$name ($visibility)"
    gh api -X PATCH "repos/$ORG/$name" --silent \
      -F allow_merge_commit=false -F allow_rebase_merge=false -F allow_squash_merge=true \
      -f squash_merge_commit_title=PR_TITLE -f squash_merge_commit_message=PR_BODY \
      -F allow_auto_merge=true -F delete_branch_on_merge=true -F allow_update_branch=true
    gh api -X PUT "repos/$ORG/$name/vulnerability-alerts" --silent
    gh api -X PUT "repos/$ORG/$name/automated-security-fixes" --silent
    if [ "$visibility" = "PUBLIC" ]; then
      gh api -X PATCH "repos/$ORG/$name" --silent --input - <<'JSON'
{"security_and_analysis": {"secret_scanning": {"status": "enabled"},
 "secret_scanning_push_protection": {"status": "enabled"}}}
JSON
    fi
  done
}

apply_org() {
  gh api -X PUT "orgs/$ORG/actions/permissions" --silent \
    -f enabled_repositories=all -f allowed_actions=all -F sha_pinning_required=true
  gh api -X PATCH "orgs/$ORG" --silent \
    -F dependabot_alerts_enabled_for_new_repositories=true \
    -F dependabot_security_updates_enabled_for_new_repositories=true \
    -F dependency_graph_enabled_for_new_repositories=true
  echo "Two-factor authentication: require it in the organization settings (no API)."
}

upsert_ruleset() {
  local file=$1 body name id
  body=$(cat "$file")
  if [[ "$body" == *RELEASE_APP_ID* ]]; then
    : "${RELEASE_APP_ID:?set RELEASE_APP_ID for $file}"
    body=$(jq --argjson id "$RELEASE_APP_ID" '.bypass_actors |= map(.actor_id = $id)' <<<"$body")
  fi
  name=$(jq -r .name <<<"$body")
  id=$(gh api "orgs/$ORG/rulesets" --jq ".[] | select(.name == \"$name\") | .id")
  if [ -n "$id" ]; then
    gh api -X PUT "orgs/$ORG/rulesets/$id" --silent --input - <<<"$body"
    echo "updated ruleset $name ($id)"
  else
    gh api -X POST "orgs/$ORG/rulesets" --silent --input - <<<"$body"
    echo "created ruleset $name"
  fi
}

case "${1:-}" in
  repos) apply_repos ;;
  org) apply_org ;;
  rulesets) for f in "$here"/rulesets/*.json; do upsert_ruleset "$f"; done ;;
  activate)
    for name in main release-tags; do
      id=$(gh api "orgs/$ORG/rulesets" --jq ".[] | select(.name == \"$name\") | .id")
      gh api -X PUT "orgs/$ORG/rulesets/$id" --silent -f enforcement=active
      echo "ruleset $name is active"
    done
    ;;
  *)
    sed -n '2,7p' "$0"
    exit 2
    ;;
esac

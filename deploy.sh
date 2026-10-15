#!/usr/bin/env bash
# Deploys Spec Keeper to Cloud Run. Secrets live in Secret Manager; create them once with
#   printf %s "$VALUE" | gcloud secrets create NAME --data-file=-
# for: slack-bot-token, slack-signing-secret, github-token, ci-secret.
# .gcloudignore keeps .env and .data/ out of the upload.
# --no-invoker-iam-check makes the service public (Slack, CI and the landing page call it) without an
# allUsers binding, which organisations with domain-restricted sharing refuse.
set -euo pipefail
: "${GCP_PROJECT:?set GCP_PROJECT}"
REGION="${GCP_LOCATION:-asia-southeast1}"
SA="speckeeper@${GCP_PROJECT}.iam.gserviceaccount.com"

gcloud run deploy speckeeper --quiet \
  --project "$GCP_PROJECT" --region "$REGION" --source . \
  --service-account "$SA" \
  --no-invoker-iam-check \
  --min-instances 1 --max-instances 2 --no-cpu-throttling \
  --set-env-vars "GCP_PROJECT=${GCP_PROJECT},GCP_LOCATION=${REGION},GEMINI_LOCATION=global,SPECKEEPER_STORE=firestore,FIRST_REQ=${FIRST_REQ:-1},GITHUB_REPO=${GITHUB_REPO:-},PRD_DOC_ID=${PRD_DOC_ID:-},APPROVERS=${APPROVERS:-},DEMO_SLACK_INVITE=${DEMO_SLACK_INVITE:-},DEMO_VIDEO_URL=${DEMO_VIDEO_URL:-},CONTACT_EMAIL=${CONTACT_EMAIL:-},DEMO_NOTES_URL=${DEMO_NOTES_URL:-},DEMO_CHANNELS=${DEMO_CHANNELS:-}" \
  --set-secrets "SLACK_BOT_TOKEN=slack-bot-token:latest,SLACK_SIGNING_SECRET=slack-signing-secret:latest,GITHUB_TOKEN=github-token:latest,CI_SECRET=ci-secret:latest"

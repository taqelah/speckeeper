#!/usr/bin/env bash
# Deploys Threadline to Cloud Run. Secrets live in Secret Manager; create them once with
#   printf %s "$VALUE" | gcloud secrets create NAME --data-file=-
# for: slack-bot-token, slack-signing-secret, github-token, ci-secret.
# .gcloudignore keeps .env and .data/ out of the upload.
set -euo pipefail
: "${GCP_PROJECT:?set GCP_PROJECT}"
REGION="${GCP_LOCATION:-asia-southeast1}"
SA="threadline@${GCP_PROJECT}.iam.gserviceaccount.com"

gcloud run deploy threadline \
  --project "$GCP_PROJECT" --region "$REGION" --source . \
  --service-account "$SA" \
  --allow-unauthenticated \
  --min-instances 1 --no-cpu-throttling \
  --set-env-vars "GCP_PROJECT=${GCP_PROJECT},GCP_LOCATION=${REGION},GEMINI_LOCATION=global,THREADLINE_STORE=firestore,FIRST_REQ=${FIRST_REQ:-1},GITHUB_REPO=${GITHUB_REPO:-},PRD_DOC_ID=${PRD_DOC_ID:-}" \
  --set-secrets "SLACK_BOT_TOKEN=slack-bot-token:latest,SLACK_SIGNING_SECRET=slack-signing-secret:latest,GITHUB_TOKEN=github-token:latest,CI_SECRET=ci-secret:latest"

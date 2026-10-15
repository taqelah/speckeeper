#!/usr/bin/env bash
# Deploys the "why?" agent (agents/speckeeper_engine) to Vertex AI Agent Engine, running as the speckeeper
# service account and reading the same Firestore. The first run creates the engine and prints its name;
# run again with AGENT_ENGINE_ID set to update it (and so the agent knows its own Memory Bank). Then set
# AGENT_ENGINE=<name> on Cloud Run (deploy.sh) to answer questions there; the local agent is the fallback.
set -euo pipefail
: "${GCP_PROJECT:?set GCP_PROJECT}"
REGION="${ENGINE_REGION:-asia-southeast1}"
SA="speckeeper@${GCP_PROJECT}.iam.gserviceaccount.com"
DIR=agents/speckeeper_engine

# Generated for the build only (gitignored): the runtime's dependencies and settings, no secrets.
uv export --no-dev --no-hashes --format requirements-txt | grep -v '^-e\|^\.' > "$DIR/requirements.txt"
cat > "$DIR/.env" <<ENV
GCP_PROJECT=${GCP_PROJECT}
ENGINE_REGION=${REGION}
GEMINI_LOCATION=global
FIRST_REQ=${FIRST_REQ:-7}
GEMINI_FLASH_MODEL=${GEMINI_FLASH_MODEL:-gemini-3.8-flash}
GEMINI_PRO_MODEL=${GEMINI_PRO_MODEL:-gemini-3.1-pro-preview}
ENV
# Agent Engine refuses empty values; on the first deploy the engine has no ID yet.
[ -n "${AGENT_ENGINE_ID:-}" ] && echo "AGENT_ENGINE_ID=${AGENT_ENGINE_ID}" >> "$DIR/.env"

${ADK:-uv run adk} deploy agent_engine \
  --project "$GCP_PROJECT" --region "$REGION" \
  --display_name "Spec Keeper why-agent" \
  --description "Answers why-questions about requirements from Firestore, with sources" \
  --service_account "$SA" \
  --extra_packages src/speckeeper \
  ${AGENT_ENGINE_ID:+--agent_engine_id "$AGENT_ENGINE_ID"} \
  "$DIR"

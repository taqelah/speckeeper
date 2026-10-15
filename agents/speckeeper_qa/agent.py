"""Entry point for the ADK web UI: `uv run adk web agents` from the repo root, then pick
speckeeper_qa. It reads the same local store the Slack server uses."""

from speckeeper.agent import build_agent, use_vertex
from speckeeper.config import Settings
from speckeeper.store import FileStore

_s = Settings.from_env()
use_vertex(_s.gcp_project, _s.gemini_location)
root_agent = build_agent(FileStore(".data/store.json", _s.first_req), _s.flash_model)

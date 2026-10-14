"""Entry point for the ADK web UI: `uv run adk web agents` from the repo root, then pick
threadline_qa. It reads the same local store the Slack server uses."""

from threadline.agent import build_agent, use_vertex
from threadline.config import Settings
from threadline.store import FileStore

_s = Settings.from_env()
use_vertex(_s.gcp_project, _s.gemini_location)
root_agent = build_agent(FileStore(".data/store.json", _s.first_req), _s.pro_model)

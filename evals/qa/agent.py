"""The agent under evaluation: the production "why?" agent, reading the fixed project instead of Firestore."""

import os

from speckeeper.agent import build_agent, use_vertex

from .fixture import fixture_store

use_vertex(os.environ["GCP_PROJECT"], os.environ.get("GEMINI_LOCATION", "global"))
root_agent = build_agent(fixture_store(), os.environ.get("GEMINI_FLASH_MODEL", "gemini-3.8-flash"))

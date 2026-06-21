"""Ensure the repo root (flat modules: config, schemas, agents, band/, ...) is
importable when pytest collects tests from the tests/ subdirectory."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

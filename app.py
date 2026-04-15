# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
# Compatibility shim — keeps `uvicorn app:app`, `from app import app`, and all
# existing test imports (including private names) working without modification.
# The real implementation lives in skiff/app.py (proper installable package).
import importlib
import sys

_real = importlib.import_module("skiff.app")
sys.modules[__name__] = _real  # replace this module entry with the real one

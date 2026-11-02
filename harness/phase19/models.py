"""Deprecated module compatibility; single semantic implementation."""
import importlib as _importlib
import sys as _sys
_sys.modules[__name__] = _importlib.import_module("harness.research_quality.models")

"""Deprecated module alias; authoritative semantic implementation only."""
import importlib as _importlib
import sys as _sys
_sys.modules[__name__] = _importlib.import_module("harness.workflow.context_spine")

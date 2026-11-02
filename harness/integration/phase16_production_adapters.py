"""Deprecated production adapter module; semantic implementation owns authority."""
import importlib as _importlib
import sys as _sys
_sys.modules[__name__] = _importlib.import_module("harness.integration.workflow_production_adapters")

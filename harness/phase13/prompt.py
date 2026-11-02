"""Deprecated module alias; implementation split by actual responsibility."""
import importlib as _importlib
import sys as _sys
_sys.modules[__name__] = _importlib.import_module("harness.executor.profile_prompt")

"""Deprecated import compatibility; implementation lives in runtime_resources.gpu_coordination."""
import importlib as _importlib
import sys as _sys
_sys.modules[__name__] = _importlib.import_module('harness.runtime_resources.gpu_coordination.lease')

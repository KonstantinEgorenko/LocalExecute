"""Deprecated import compatibility; implementation lives in runtime_resources.gpu_coordination."""
import importlib as _importlib
import sys as _sys
if __name__ == '__main__':
    raise SystemExit(_importlib.import_module('harness.runtime_resources.gpu_coordination.cli').main())
_sys.modules[__name__] = _importlib.import_module('harness.runtime_resources.gpu_coordination.cli')

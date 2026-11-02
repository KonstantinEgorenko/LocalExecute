"""Deprecated module compatibility; single semantic implementation."""
import importlib as _importlib
import sys as _sys
if __name__ == "__main__":
    raise SystemExit(_importlib.import_module("harness.gpu_acceleration.launcher").main())
_sys.modules[__name__] = _importlib.import_module("harness.gpu_acceleration.launcher")

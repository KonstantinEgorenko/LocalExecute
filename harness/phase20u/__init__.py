"""Deprecated package compatibility; implementation uses semantic names."""
from harness.gpu_acceleration import *
from harness.gpu_acceleration import __all__

def __getattr__(name):
    import importlib
    current = importlib.import_module("harness.gpu_acceleration")
    aliases = {'Phase20UPolicy': 'GpuAccelerationPolicy'}
    return getattr(current, aliases.get(name, name))

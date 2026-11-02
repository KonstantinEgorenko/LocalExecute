"""Legacy bootstrap entrypoint; use semantic model-runtime bootstrap."""
import runpy
from pathlib import Path
runpy.run_path(str(Path(__file__).resolve().parents[2] / "model_runtime" / "bootstrap/sitecustomize.py"))

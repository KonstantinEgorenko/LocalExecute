"""Public command facade over the existing production launcher."""
import argparse
from . import __version__

def main(argv=None):
    parser = argparse.ArgumentParser(prog='localexecute', description='LocalExecute — Autonomous Agent Harness')
    parser.add_argument('--version', action='version', version=__version__)
    parser.add_argument('command', choices=('run',))
    parser.add_argument('launcher_args', nargs=argparse.REMAINDER, help='Existing production launcher arguments: --root, --module, then module arguments')
    args = parser.parse_args(argv)
    from harness.integration.production_launcher import main as launch
    return launch(args.launcher_args)

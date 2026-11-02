"""Opt-in external supervisor CLI. Explicit invocation parameters only."""
import argparse
import json
from pathlib import Path
from .supervision.service import prepare, supervise, reconcile


def main(argv=None):
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest='mode',required=True)
    run=sub.add_parser('run')
    run.add_argument('--root',type=Path,required=True)
    run.add_argument('--evaluation-id',required=True)
    run.add_argument('--manifest-fingerprint',required=True)
    run.add_argument('--deadline-seconds',type=float,required=True)
    run.add_argument('--grace-seconds',type=float,required=True)
    run.add_argument('--heartbeat-seconds',type=float,required=True)
    run.add_argument('--cwd',type=Path,required=True)
    run.add_argument('--attempt-claim',type=Path)
    run.add_argument('command',nargs=argparse.REMAINDER)
    rec=sub.add_parser('reconcile');rec.add_argument('--invocation-root',type=Path,required=True)
    args=parser.parse_args(argv)
    if args.mode=='run':
        command=args.command[1:] if args.command and args.command[0]=='--' else args.command
        root=prepare(args.root,args.evaluation_id,args.manifest_fingerprint,command,
            args.deadline_seconds,args.grace_seconds,args.heartbeat_seconds,args.cwd,
            attempt_claim=args.attempt_claim)
        print(json.dumps({'invocation_root':str(root)}),flush=True)
        result=supervise(root)
    else:result=reconcile(args.invocation_root)
    print(json.dumps(result),flush=True)
    payload=result['payload']
    return 0 if payload.get('exit_code')==0 and payload.get('evaluation_outcome')=='PROCESS_EXIT_ZERO' else 2


if __name__=='__main__':raise SystemExit(main())

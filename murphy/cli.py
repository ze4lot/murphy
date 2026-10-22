"""User-facing setup and multi-project commands."""
import argparse
import json
import math
import os
import sqlite3
import subprocess
import sys

from . import __version__, bridge
from .manager import Manager
from . import monitor


def main():
    # Preserve the prototype's explicit configuration commands and saved state.
    if '--config' in sys.argv[1:]:
        return bridge.main()
    parser = argparse.ArgumentParser(description='Run GitHub tasks locally, one at a time per project.')
    parser.add_argument('--version', action='version', version='murphy ' + __version__)
    parser.add_argument('--state-dir', help='Private installation state (default: ~/.local/state/murphy/manager)')
    sub = parser.add_subparsers(dest='action', required=True)
    init = sub.add_parser('init', help='Use the currently signed-in gh account')
    init.add_argument('--separate-account', action='store_true', help='Sign in a separate executor account')
    for name in ('gh', 'git', 'codex'):
        init.add_argument('--' + name, default=name, help='Executable name or path')
    auth = sub.add_parser('auth', help='Inspect or restore executor authentication')
    auth_actions = auth.add_subparsers(dest='auth_action', required=True)
    auth_actions.add_parser('status')
    auth_actions.add_parser('login', help='Sign in again as the same saved executor (worker must be stopped)')
    projects = sub.add_parser('projects', help='Discover and connect repositories')
    actions = projects.add_subparsers(dest='project_action', required=True)
    actions.add_parser('available', help='List repositories accessible to your account')
    actions.add_parser('list', help='List connected projects')
    add = actions.add_parser('add', help='Connect a repository; clone it if needed')
    add.add_argument('repository')
    add.add_argument('--checkout', help='Use an existing local checkout without switching or cleaning it')
    add.add_argument('--base', help='Task base branch (default: GitHub default branch)')
    add.add_argument('--model', help='Optional Codex model for new tasks')
    add.add_argument('-i', '--interval', type=int, default=60,
                     help='Poll this repository every N seconds (default: 60, minimum: 1)')
    run = sub.add_parser('run', help='Watch all connected projects and execute the shared queue')
    run.add_argument('-m', '--monitor', action='store_true', help='Show a live terminal table while running')
    run.add_argument('--once', action='store_true')
    run.add_argument('--poll-seconds', type=float,
                     help='Override all saved project intervals for this run')
    run.add_argument('--timeout', type=float, default=600)
    run.add_argument('--max-parallel-projects', type=int, default=2,
                     help='Maximum simultaneously executing projects (default: 2; use 1 for serial work)')
    register = sub.add_parser('register', help='Register a clean task checkout and issue/PR aliases')
    register.add_argument('name')
    register.add_argument('--project', required=True, help='Connected owner/repository')
    register.add_argument('--cwd', required=True)
    register.add_argument('--branch', required=True)
    register.add_argument('--item', type=int, action='append', default=[])
    register.add_argument('--model')
    register.add_argument('--reasoning', choices=bridge.EFFORTS)
    enqueue = sub.add_parser('enqueue', help='Submit a local command to the shared queue')
    enqueue.add_argument('task')
    enqueue.add_argument('verb', choices=('start', 'continue'))
    enqueue.add_argument('--project', required=True)
    enqueue.add_argument('--prompt', required=True)
    ingest = sub.add_parser('ingest-comment', help='Refetch a comment through the normal authorization checks')
    ingest.add_argument('id', type=int)
    ingest.add_argument('--project', required=True)
    for verb in ('status', 'pause', 'resume'):
        sub.add_parser(verb)
    view = sub.add_parser('monitor', help='Watch local worker activity without starting or changing work')
    view.add_argument('--once', action='store_true', help='Print one snapshot and exit (also the default for pipes)')
    cancel = sub.add_parser('cancel')
    cancel.add_argument('id', type=int, help='Global command ID from status or a report')
    resolve = sub.add_parser('resolve')
    resolve.add_argument('id', type=int)
    resolve.add_argument('--outcome', required=True, choices=('succeeded', 'failed', 'cancelled'))
    resolve.add_argument('--note', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    manager = None
    try:
        if args.action == 'run' and ((args.poll_seconds is not None and
                                     (not math.isfinite(args.poll_seconds) or args.poll_seconds < 1)) or
                                    args.timeout <= 0):
            raise ValueError('Poll interval >= 1s and positive timeout required.')
        if args.action == 'monitor':
            return monitor.watch(args.state_dir, args.once)
        if args.action == 'run' and args.monitor:
            options = ['--timeout', str(args.timeout), '--max-parallel-projects', str(args.max_parallel_projects)]
            if args.poll_seconds is not None:
                options += ['--poll-seconds', str(args.poll_seconds)]
            if args.once:
                options.append('--once')
            return monitor.run_with_monitor(args.state_dir, options)
        manager = Manager(args.state_dir)
        if args.action == 'init':
            with bridge.lock(manager):
                owner = manager.init(args.gh, args.git, args.codex, args.separate_account)
            print('Command owner: ' + owner + '\nExecutor: ' + manager.account.settings['login'])
        elif args.action == 'auth':
            if args.auth_action == 'login':
                manager.login()
            else:
                status = manager.auth_status()
                print(json.dumps(status, indent=2))
                return 0 if status['authenticated'] else 1
        elif args.action == 'projects':
            if args.project_action == 'available':
                print(json.dumps(manager.available(), indent=2))
            elif args.project_action == 'list':
                print(json.dumps(manager.status()['projects'], indent=2))
            else:
                # Exclude changes to the project registry during a worker run.
                with bridge.lock(manager):
                    print('Connected ' + manager.add(args.repository, args.checkout, args.base, args.model,
                                                     args.interval))
        elif args.action == 'run':
            manager.poll_seconds = args.poll_seconds
            manager.timeout = args.timeout
            manager.max_parallel_projects = args.max_parallel_projects
            return manager.run(args.once)
        elif args.action == 'status':
            print(json.dumps(manager.status(), indent=2))
        elif args.action == 'register':
            manager.register(args.project, args.name, args.cwd, args.branch, args.item, args.model, args.reasoning)
        elif args.action == 'enqueue':
            print(manager.enqueue(args.project, args.task, args.verb, args.prompt))
        elif args.action == 'ingest-comment':
            print(json.dumps(manager.ingest(args.project, args.id)))
        elif args.action == 'resolve':
            manager.resolve(args.id, args.outcome, args.note)
        else:
            manager.load()
            manager.sync_queue()
            manager.control(args.action, str(getattr(args, 'id', '')))
    except (ValueError, RuntimeError, OSError, sqlite3.Error, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        if manager is not None:
            manager.close()
    return 0

"""Explicit, fail-closed binding to an existing local Codex tmux pane."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile

CONFIG_ROOT = Path(os.environ.get('SUPERVISOR_CONFIG_DIR', str(Path.home() / '.config/codex-supervisor')))
DEFAULT_INCLUDED_SESSIONS_FILE = CONFIG_ROOT / 'included-sessions'
DEFAULT_EXCLUDED_SESSIONS_FILE = CONFIG_ROOT / 'excluded-sessions'
REGISTRY = CONFIG_ROOT / 'frontends.json'
THREAD = re.compile(r'^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$', re.I)
ROLLOUT = re.compile(r'rollout-.*-([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.jsonl$')


def session_ids(path: Path) -> set[str]:
    try:
        return {s for line in path.read_text().splitlines()
                if THREAD.fullmatch(s := line.split('#', 1)[0].strip().lower())}
    except FileNotFoundError:
        return set()


def is_session_excluded(thread: str) -> bool:
    excluded = Path(os.environ.get('TLS_EXCLUDED_SESSIONS_FILE', str(DEFAULT_EXCLUDED_SESSIONS_FILE)))
    included = Path(os.environ.get('TLS_INCLUDED_SESSIONS_FILE', str(DEFAULT_INCLUDED_SESSIONS_FILE)))
    return thread in session_ids(excluded) or thread not in session_ids(included)


def tmux(*args: str, input: str | None = None, socket: str | None = None) -> str:
    socket = socket or os.environ.get('SUPERVISOR_TMUX_SOCKET') or os.environ.get('TMUX', '').split(',')[0]
    command = ['tmux', '-S', socket] if socket else ['tmux']
    return subprocess.run([*command, *args], input=input, check=True, capture_output=True,
                          text=True, timeout=5).stdout


def pane_info(pane: str, *, socket: str | None = None) -> dict:
    if not re.fullmatch(r'%[0-9]+', pane):
        raise ValueError('use an exact tmux pane ID such as %3')
    text = tmux('display-message', '-p', '-t', pane, '#{pane_id}|#{pane_pid}|#{pane_tty}|#{pane_dead}|#{socket_path}', socket=socket)
    identity, pid, tty, dead, actual_socket = text.strip().split('|')
    if identity != pane or dead != '0':
        raise RuntimeError('original tmux pane is unavailable')
    return {'pane': identity, 'pid': int(pid), 'tty': tty.removeprefix('/dev/'), 'socket': actual_socket}


def process_info(pid: int) -> dict:
    root = Path('/proc') / str(pid)
    if root.stat().st_uid != os.getuid():
        raise RuntimeError('Codex PID is not owned by this user')
    fields = (root / 'stat').read_text().rsplit(')', 1)[1].split()
    if fields[0] == 'Z':
        raise RuntimeError('Codex PID has exited')
    args = [p.decode(errors='replace') for p in (root / 'cmdline').read_bytes().split(b'\0') if p]
    if not args or Path(args[0]).name not in {'codex', 'codex.exe'} or '--remote' in args:
        raise RuntimeError('binding requires a local Codex CLI PID, not a remote client')
    tty = os.readlink(root / 'fd/0').removeprefix('/dev/')
    rollouts = set()
    for descriptor in (root / 'fd').iterdir():
        try:
            path = os.readlink(descriptor)
        except OSError:
            continue
        if match := ROLLOUT.search(path):
            rollouts.add(match.group(1).lower())
    return {'parent': int(fields[1]), 'start': fields[19], 'tty': tty, 'threads': rollouts}


def descendant(pid: int, ancestor: int) -> bool:
    for _ in range(64):
        if pid == ancestor:
            return True
        if pid <= 1:
            return False
        fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
        pid = int(fields[1])
    return False


def verify_record(thread: str, record: dict) -> dict:
    pane = pane_info(record['terminal_name'], socket=record.get('tmux_socket'))
    pid = int(record['codex_pid'])
    process = process_info(pid)
    if process['threads'] != {thread} or process['tty'] != pane['tty'] or not descendant(pid, pane['pid']):
        raise RuntimeError('Codex PID, transcript, and pane do not identify one original conversation')
    token = hashlib.sha256(f"{pid}:{process['start']}:{pane['pane']}:{pane['tty']}".encode()).hexdigest()
    if record.get('token') and token != record['token']:
        raise RuntimeError('original Codex PID or pane changed')
    result = {'codex_pid': pid, 'shell_pid': pane['pid'], 'tty': pane['tty'], 'backend': 'tmux',
              'terminal_name': pane['pane'], 'token': token, 'binding_source': 'explicit'}
    if pane.get('socket'):
        result['tmux_socket'] = pane['socket']
    return result


def _registry() -> dict:
    try:
        result = json.loads(REGISTRY.read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(result, dict):
        raise RuntimeError('invalid frontend registry')
    return result


def active_session_terminals() -> dict:
    result = {}
    records = _registry()
    for thread, record in records.items():
        if is_session_excluded(thread):
            continue
        # Refuse ambiguous registrations even if their process IDs differ.
        if sum((r.get('tmux_socket'), r.get('terminal_name')) == (record.get('tmux_socket'), record.get('terminal_name')) for r in records.values()) != 1:
            continue
        try:
            result[thread] = verify_record(thread, record)
        except (OSError, ValueError, KeyError, IndexError, RuntimeError, subprocess.SubprocessError):
            continue
    return result


def atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def bind(thread: str, pane: str, pid: int) -> dict:
    if not THREAD.fullmatch(thread):
        raise ValueError('invalid thread ID')
    thread = thread.lower()
    record = verify_record(thread, {'terminal_name': pane, 'codex_pid': pid})
    CONFIG_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (CONFIG_ROOT / 'lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        records = _registry()
        if any(t != thread and (r.get('tmux_socket'), r.get('terminal_name')) == (record.get('tmux_socket'), pane) for t, r in records.items()):
            raise RuntimeError('pane is already bound; explicitly unbind its previous thread first')
        records[thread] = record
        atomic(REGISTRY, json.dumps(records) + '\n')
        allowed = session_ids(DEFAULT_INCLUDED_SESSIONS_FILE) | {thread}
        atomic(DEFAULT_INCLUDED_SESSIONS_FILE, '\n'.join(sorted(allowed)) + '\n')
    return record


def deliver_tmux_session_turn(pane: str, message: str, *, socket: str | None = None) -> None:
    pane_info(pane, socket=socket)
    if '\x1b' in message or '\r' in message or '\0' in message:
        raise ValueError('control characters in supervision message')
    name = 'codex-supervisor-' + secrets.token_hex(12)
    execute = lambda *args, **kwargs: tmux(*args, **kwargs, socket=socket) if socket else tmux(*args, **kwargs)
    try:
        execute('load-buffer', '-b', name, '-', input=message)
        if execute('save-buffer', '-b', name, '-') != message:
            raise RuntimeError('tmux buffer verification failed')
        execute('send-keys', '-t', pane, 'C-u')
        execute('paste-buffer', '-p', '-d', '-b', name, '-t', pane)
        execute('send-keys', '-t', pane, 'Enter')
        execute('capture-pane', '-p', '-t', pane, '-S', '-20')
    finally:
        try:
            execute('delete-buffer', '-b', name)
        except subprocess.SubprocessError:
            pass


# Interface retained for optional original-window bridge adapters. The public
# default never falls back to a hidden app-server turn.
def cursor_terminal_runtime(shell_pid: int, *, codex_pid: int):
    return None


def deliver_cursor_turn(runtime: dict, message: str) -> None:
    raise RuntimeError('Cursor bridge is not included; use the standalone tmux backend')


def submit_cursor_turn(runtime: dict) -> None:
    raise RuntimeError('Cursor bridge is not included; use the standalone tmux backend')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    registration = sub.add_parser('bind')
    registration.add_argument('--thread', required=True)
    registration.add_argument('--pane', required=True)
    registration.add_argument('--pid', type=int, required=True)
    removal = sub.add_parser('unbind')
    removal.add_argument('--thread', required=True)
    sub.add_parser('list')
    args = parser.parse_args()
    if args.command == 'bind':
        result = bind(args.thread, args.pane, args.pid)
    elif args.command == 'unbind':
        CONFIG_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (CONFIG_ROOT / 'lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            records = _registry()
            records.pop(args.thread, None)
            atomic(REGISTRY, json.dumps(records) + '\n')
            allowed = session_ids(DEFAULT_INCLUDED_SESSIONS_FILE) - {args.thread}
            atomic(DEFAULT_INCLUDED_SESSIONS_FILE, '\n'.join(sorted(allowed)) + '\n')
        result = {'unbound': args.thread}
    else:
        result = active_session_terminals()
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()

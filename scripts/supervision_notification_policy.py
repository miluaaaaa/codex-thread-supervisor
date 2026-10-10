"""Five-cycle TLS progress cadence and one terminal notification per supervisor."""
import fcntl
import functools
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import time

THREAD_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)
SUPERVISION_TURN = re.compile(r"TLS[_\s]*SUPERVISION[_\s]*(?:CONTINUE|STATE|COMPLETE)|继续监督完成", re.I)
ACTIVATION = re.compile(r"^(?:请|帮我|麻烦)?\s*(?:开启|启动|启用|打开)监督器")
PROGRESS_CYCLES = 5
CADENCE = 'cycles5-answer-terminal-v2'


def _details(session_id, user_message, answer):
    match = THREAD_ID.search(str(session_id or ""))
    thread = match.group(0).lower() if match else str(session_id or "unknown")
    root = Path(os.environ.get("TLS_SUPERVISOR_STATE_DIR", str(Path.home() / ".local/state/codex-supervisor")))
    try:
        state = json.loads((root / (thread + ".json")).read_text()) if match else {}
    except (OSError, ValueError):
        state = {}
    request = str(user_message or "").strip()
    text = request + "\n" + str(answer or "")
    activation = bool(ACTIVATION.search(request))
    supervised_turn = bool(SUPERVISION_TURN.search(text))
    # Reminder instructions mention COMPLETE on every continuation. Only an
    # actual answer marker can finish the notification episode.
    terminal = bool(re.search(r"^\s*TLS_SUPERVISION_COMPLETE\b", str(answer or ""), re.I | re.M))
    terminal = terminal or bool(re.match(r"^(?:请|帮我)?\s*(?:关闭|停止|结束)监督器", request))
    terminal = terminal or (supervised_turn and state.get("active") is False and bool(state.get("stopped_at")))
    managed = activation or terminal or supervised_turn or state.get("active") is True
    episode = str(state.get("instance_id") or state.get("started_at") or "remote")
    return thread, state, managed, activation, terminal, episode


def _ledger():
    root = Path(os.environ.get("TLS_SUPERVISOR_NOTIFY_DIR", str(Path.home() / ".local/state/codex-supervisor-notifications")))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _read(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def _fingerprint(user_message, answer):
    return hashlib.sha256((str(user_message) + '\n' + str(answer)).encode()).hexdigest()


def suppress_supervision_notification(session_id: object = "", user_message: object = "", answer: object = "", *, delivery: bool = False, cycle_key: object = None) -> bool:
    thread, state, managed, activation, terminal, episode = _details(session_id, user_message, answer)
    if not managed:
        return False
    if activation:
        return True
    root = _ledger()
    with (root / 'lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = root / 'state.json'
        ledger = _read(path)
        entry = ledger.get(thread, {})
        if entry.get('episode') != episode or entry.get('cadence') != CADENCE:
            entry = {'episode': episode, 'cadence': CADENCE, 'cycles': 0, 'seen': [], 'terminal_sent': False}
            ledger[thread] = entry
            _save(path, ledger)
        if terminal:
            return bool(entry.get('terminal_sent')) if episode != 'remote' else False
        if entry.get('terminal_sent') and episode != 'remote':
            return True
        supervised_turn = bool(SUPERVISION_TURN.search(str(user_message) + '\n' + str(answer)))
        if not supervised_turn:
            return True
        if delivery:
            # The source admits every fifth cycle; never count it again here.
            delivery_key = str(cycle_key) if cycle_key is not None else _fingerprint(user_message, answer)
            return entry.get('delivered_key') == delivery_key
        if cycle_key is not None:
            key = str(cycle_key)
            if key not in entry['seen']:
                entry['seen'] = (entry['seen'] + [key])[-100:]
                entry['cycles'] += 1
                ledger[thread] = entry
                _save(path, ledger)
        return entry['cycles'] < PROGRESS_CYCLES


def record_supervision_notification(session_id: object = "", user_message: object = "", answer: object = "", *, delivery_key: object = None) -> None:
    """Advance cadence only after durable acceptance, so failed delivery can retry."""
    thread, state, managed, activation, terminal, episode = _details(session_id, user_message, answer)
    if not managed:
        return
    root = _ledger()
    with (root / 'lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = root / 'state.json'
        ledger = _read(path)
        entry = ledger.get(thread, {})
        if activation or entry.get('episode') != episode or entry.get('cadence') != CADENCE:
            entry = {'episode': episode, 'cadence': CADENCE, 'cycles': 0, 'seen': [], 'terminal_sent': False}
        if terminal:
            entry['terminal_sent'] = True
        else:
            entry['cycles'] = 0
            if delivery_key is not None:
                entry['delivered_key'] = str(delivery_key)
        ledger[thread] = entry
        _save(path, ledger)


def supervised_delivery(function):
    """Guard the actual sender too, so queued catch-up cannot cause a burst."""
    signature = inspect.signature(function)

    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        values = signature.bind(*args, **kwargs).arguments
        context = tuple(values.get(k, '') for k in ('session_id', 'user_message', 'answer'))
        delivery_key = values.get('event_key') or _fingerprint(context[1], context[2])
        if suppress_supervision_notification(*context, delivery=True, cycle_key=delivery_key):
            return True  # Acknowledged without sending, so durable queues do not retry.
        sent = function(*args, **kwargs)
        if sent:
            record_supervision_notification(*context, delivery_key=delivery_key)
        return sent

    return wrapped

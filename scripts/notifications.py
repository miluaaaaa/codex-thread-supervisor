"""Durable, transport-independent outbox for admitted supervision reports."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from frontends import atomic
from supervision_notification_policy import record_supervision_notification, suppress_supervision_notification


def queue_report(state: dict, nonce: str, answer: str) -> bool:
    """Count once per confirmed supervision; reset only after durable enqueue."""
    thread = state['thread_id']
    state_root = Path(os.environ.get('SUPERVISOR_STATE_DIR', str(Path.home() / '.local/state/codex-supervisor')))
    os.environ.setdefault('TLS_SUPERVISOR_STATE_DIR', str(state_root))
    request = '继续监督完成' + state['task'] + '。'
    cycle = str(state['instance_id']) + ':' + nonce
    if suppress_supervision_notification(thread, request, answer, cycle_key=cycle):
        return False
    event = hashlib.sha256((thread + ':' + cycle).encode()).hexdigest()
    outbox = Path(os.environ.get('SUPERVISOR_OUTBOX_DIR', str(state_root / 'outbox')))
    path = outbox / (event + '.json')
    if not path.exists():
        atomic(path, json.dumps({'event_id': event, 'thread_id': thread, 'instance_id': state['instance_id'],
                                'task': state['task'], 'answer': answer}, ensure_ascii=False) + '\n')
    record_supervision_notification(thread, request, answer, delivery_key=event)
    return True

"""Carry one real QQ video segment to an independent Flash recovery worker."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

from bili_companion.domain import Scope

from .flash_duty import finish_recovery
from .live import _json_response
from .routing import flash_attempt
from .video import feedback
from .video_budget import video_budget


MAX_HANDOFF_BYTES = 18 * 1024 * 1024
ENVIRONMENT_KEYS = {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR', 'LANG', 'LC_ALL'}
CONFIG_KEYS = ('state_dir', 'flash_duty_profile', 'monthly_usd', 'daily_usd',
               'reply_daily_reserve_usd', 'budget_scope')


def launch(config, payload, generation, lease, request_limit):
    settings = {key: config[key] for key in CONFIG_KEYS if key in config}
    packet = json.dumps(dict(config=settings, payload=payload, generation=generation, lease=lease,
                             request_limit=str(request_limit)),
                        allow_nan=False, ensure_ascii=False).encode('utf-8')
    if len(packet) > MAX_HANDOFF_BYTES:
        raise ValueError('Flash recovery payload exceeds limit')
    env = {key: value for key, value in os.environ.items() if key.upper() in ENVIRONMENT_KEYS}
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[2])
    options = {}
    if os.name == 'nt':
        options['creationflags'] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen([sys.executable, '-m', 'bili_companion.cognition.flash_video_recovery'],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, env=env, **options)
    # Closing stdin delivers EOF. The model runs independently once it has
    # received the complete packet, including the actual encoded segment.
    with process.stdin as pipe:
        pipe.write(packet)


def _recover(config, payload, request_limit):
    parts = [part for content in payload['contents'] for part in content['parts']]
    media = [part['inlineData'] for part in parts if 'inlineData' in part]
    if len(media) != 1 or media[0].get('mimeType') != 'video/mp4' or not media[0].get('data'):
        raise ValueError('Flash recovery requires actual video')
    data, = [json.loads(part['text']) for part in parts if 'text' in part]
    duration, modality = data['duration'], data['available_modality']
    if type(duration) not in (int, float) or not 0 < duration <= 60 or modality not in {'audio','video','mixed'}:
        raise ValueError('Invalid real segment metadata')
    scope = Scope('flash-recovery', 'video', 'qq-video')
    chunk = SimpleNamespace(start=0., end=duration, modality=modality,
                            received_at=time.time(), session_id='flash-recovery')

    def validate(engine, response):
        value = _json_response(response)
        base = {key: item for key, item in value.items()
                if key not in {'video_feedback', 'live_feedback', 'child_request'}}
        observation, _ = engine._observation(base, chunk, scope, 'flash-recovery')
        if not observation.evidence:
            raise ValueError('no_grounded_observation')
        result = feedback(engine, value['video_feedback'], observation, scope, chunk.session_id, 0, False)
        if result['comment_status'] == 'invalid':
            raise ValueError('invalid_video_feedback')

    ledger = video_budget(config, lane='qq_video')
    flash_attempt(config, payload, validate, ledger, ledger, audit_costs=True,
                  purpose='video-observe', request_limit=request_limit)


def main():
    data = sys.stdin.buffer.read(MAX_HANDOFF_BYTES + 1)
    if len(data) > MAX_HANDOFF_BYTES:
        raise ValueError('Flash recovery payload exceeds limit')
    packet = json.loads(data)
    config, payload = packet['config'], packet['payload']
    finish_recovery(config, 'video', packet['generation'], packet['lease'],
                    lambda: _recover(config, payload, packet['request_limit']))


if __name__ == '__main__':
    main()

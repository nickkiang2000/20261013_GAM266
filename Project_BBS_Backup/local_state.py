"""Local form preferences and credential-free diagnostic logs."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
import traceback


PREFERENCE_FIELDS = ('site', 'user', 'board', 'start', 'count', 'folder', 'theme', 'direction')
LOCAL_DIRECTORY = Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'BBS Downloader'
SETTINGS = LOCAL_DIRECTORY / 'form.json'
RATE_LIMIT_FILE = LOCAL_DIRECTORY / 'rate_limits.json'
LOG_DIRECTORY = Path(__file__).resolve().parent / 'log'


def load_preferences():
    try:
        data = json.loads(SETTINGS.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: data[key] for key in PREFERENCE_FIELDS if isinstance(data.get(key), str)}


def save_preferences(data):
    values = {key: data[key] for key in PREFERENCE_FIELDS
              if isinstance(data.get(key), str) and len(data[key]) <= 4096}
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    temp = SETTINGS.with_name(f'form-{secrets.token_hex(5)}.tmp')
    try:
        temp.write_text(json.dumps(values, ensure_ascii=False), encoding='utf-8')
        temp.replace(SETTINGS)
    finally:
        temp.unlink(missing_ok=True)


def load_rate_limits():
    try:
        data = json.loads(RATE_LIMIT_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_rate_limits(data):
    RATE_LIMIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = RATE_LIMIT_FILE.with_name(f'rate-limits-{secrets.token_hex(5)}.tmp')
    try:
        temp.write_text(json.dumps(data), encoding='utf-8')
        temp.replace(RATE_LIMIT_FILE)
    finally:
        temp.unlink(missing_ok=True)


class JobLog:
    def __init__(self, folder=None):
        timestamp = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S')
        directory = LOG_DIRECTORY if folder is None else folder / 'log'
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f'{timestamp}-{secrets.token_hex(3)}.log'

    def event(self, event, **fields):
        timestamp = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')
        try:
            with self.path.open('a', encoding='utf-8') as log:
                log.write(json.dumps({'time': timestamp, 'event': event, **fields}, ensure_ascii=False) + '\n')
        except OSError:
            # A diagnostic write failure must not mask the original result.
            pass

    def error(self, stage, exc, *, index=None, aid=None):
        # Record stack locations without exception messages, source lines or locals.
        # The SDK can place account prompts and article text in exception messages.
        stack = [f'{Path(frame.filename).name}:{frame.lineno}:{frame.name}'
                 for frame in traceback.extract_tb(exc.__traceback__)]
        self.event('error', stage=stage, error=type(exc).__name__, index=index, aid=aid, stack=stack)

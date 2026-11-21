"""Local form preferences and credential-free diagnostic logs."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
import traceback


PREFERENCE_FIELDS = ('site', 'user', 'board', 'start', 'count', 'folder', 'theme', 'direction', 'repair_root')
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


def save_reading_capture(board, aid, mode, samples, diagnostic, reason):
    """Save only recognized article screens, with raw replies for offline replay."""
    from base64 import b64encode
    from html import escape
    from ansi_style import ANSI_STYLE, render_run

    directory = LOG_DIRECTORY / 'readings'
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S')
    name = f'{stamp}-{secrets.token_hex(3)}'
    data_path = directory / f'{name}.json'
    html_path = directory / f'{name}.html'
    frames = [{**sample, 'raw': b64encode(sample['raw']).decode('ascii')} for sample in samples]
    payload = dict(board=board, aid=aid, mode=mode, reason=reason, diagnostic=diagnostic, frames=frames)
    sections = []
    for i, sample in enumerate(samples, 1):
        rows = sample['screen'].split('\n')
        rendered = []
        for index, row in enumerate(rows):
            colors = sample['colors']
            runs = colors[index] if colors is not None and index < len(colors) else []
            if runs and ''.join(value for value, _ in runs) == row:
                rendered.append(''.join(render_run(value, style) for value, style in runs))
            else:
                rendered.append(escape(row))
        sections.append(f'<h2>畫面 {i} · {escape(sample["key"])} · {sample["height"]} 列</h2>'
                        '<pre class="color-reference">' + '\n'.join(rendered) + '</pre>')
    overlap = diagnostic.get('numbering', {}).get('screen_overlap') or {}
    first = overlap.get('first_failed_seam') or {}
    latest = overlap.get('last_failed_seam') or {}
    snapshots = [(first, '最後確認成功的畫面', '第一次接合失敗的畫面')]
    if latest and latest != first:
        snapshots.append((latest, '最近失敗前的已確認畫面', '最近一次接合失敗的畫面'))
    for seam, accepted_label, observed_label in snapshots:
        for label, key in ((accepted_label, 'accepted'), (observed_label, 'observed')):
            if seam.get(key):
                rendered = [''.join(render_run(text, style) for text, style in runs) if runs else escape(text)
                            for text, runs in seam[key]]
                sections.append(f'<h2>{label}</h2><pre class="color-reference">' + '\n'.join(rendered) + '</pre>')
    heading = escape(f'{board} · AID {aid} · {mode}')
    page = ('<!doctype html><html lang="zh-Hant"><meta charset="utf-8">'
            f'<title>文章擷取診斷 · {heading}</title><style>{ANSI_STYLE}'
            'body{background:#111;color:#ddd;margin:20px}pre{background:#000;'
            'white-space:pre;width:max-content;min-width:80ch;padding:12px}</style>'
            f'<h1>{heading}</h1><p>{escape(reason)}</p>'
            '<p>以下為程式收到的文章畫面。配套 JSON 含原始回應及配色，可供離線重播除錯。</p>'
            f'<p><a href="{name}.json">下載診斷 JSON</a></p>' + ''.join(sections) + '</html>')
    try:
        directory.mkdir(parents=True, exist_ok=True)
        data_path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        html_path.write_text(page, encoding='utf-8')
    except OSError:
        return None
    return html_path

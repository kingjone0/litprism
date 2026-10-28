"""Transport SSE decoding and volatile, untrusted QA previews; never a knowledge store."""
from __future__ import annotations

import json
import threading


def sse_data(response):
    """Decode complete SSE frames, including multiline data and UTF-8 boundaries."""
    from .tasks import checkpoint
    lines = []
    try:
        # A small provider frame must arrive before a large network buffer fills.
        for raw in response.iter_lines(chunk_size=1):
            checkpoint()
            line = raw.decode('utf-8') if isinstance(raw, bytes) else raw
            if line == '':
                if lines:
                    yield '\n'.join(lines)
                    lines.clear()
            elif line.startswith('data:'):
                value = line[5:]
                lines.append(value[1:] if value.startswith(' ') else value)
        if lines:
            yield '\n'.join(lines)
    finally:
        response.close()


class AnswerPreview:
    """Incrementally read only root answer or answer_units[i].text JSON strings.

    This lexer never repairs JSON or interprets evidence fields as answer text.
    Final parsing, protocol validation and citations still use the original bytes.
    """
    def __init__(self):
        self.stack, self.parts = [], {}
        self.started = self.ended = self.invalid = self.in_string = False
        self.escape = self.unicode = ''
        self.pending_high = None
        self.key, self.target, self.token, self.primitive = False, None, [], False
        self.units = False

    def value_path(self):
        if not self.stack:
            return ()
        frame = self.stack[-1]
        return frame['path'] + ((frame['key'] if frame['kind'] == '{' else frame['index']),)

    def complete(self):
        if self.stack:
            self.stack[-1]['expect'] = 'comma'
        else:
            self.ended = True

    def decoded(self, char):
        if self.pending_high is not None:
            if 0xDC00 <= ord(char) <= 0xDFFF:
                char = chr(0x10000 + ((self.pending_high-0xD800) << 10) + ord(char)-0xDC00)
                self.pending_high = None
            else:
                self.invalid = True
                return
        elif 0xD800 <= ord(char) <= 0xDBFF:
            self.pending_high = ord(char)
            return
        elif 0xDC00 <= ord(char) <= 0xDFFF:
            self.invalid = True
            return
        if self.key:
            self.token.append(char)
        elif self.target is not None:
            self.parts[self.target].append(char)

    def feed(self, fragment):
        for char in fragment:
            if self.invalid or self.ended:
                return
            if self.in_string:
                if self.escape == 'u':
                    self.unicode += char
                    if len(self.unicode) == 4:
                        try:
                            self.decoded(chr(int(self.unicode, 16)))
                        except ValueError:
                            self.invalid = True
                        self.escape = self.unicode = ''
                elif self.escape:
                    if char == 'u':
                        self.escape = 'u'
                    else:
                        value = {'"': '"', '\\': '\\', '/': '/', 'b': '\b', 'f': '\f',
                                 'n': '\n', 'r': '\r', 't': '\t'}.get(char)
                        if value is None:
                            self.invalid = True
                        else:
                            self.decoded(value)
                        self.escape = ''
                elif char == '\\':
                    self.escape = '\\'
                elif char == '"':
                    self.in_string = False
                    if self.pending_high is not None:
                        self.invalid = True
                    if self.key:
                        frame = self.stack[-1]
                        key = ''.join(self.token)
                        if key in frame['keys']:
                            self.invalid = True
                        frame['keys'].add(key)
                        frame['key'], frame['expect'] = key, 'colon'
                    else:
                        self.complete()
                elif ord(char) < 32:
                    self.invalid = True
                else:
                    self.decoded(char)
                continue
            if not self.started:
                if char != '{':
                    continue  # A fenced JSON response can have a non-JSON prefix.
                self.started = True
            if self.primitive:
                if char not in ',}] \r\n\t':
                    continue
                self.primitive = False
                self.complete()
            if char.isspace():
                continue
            if char == '"':
                self.in_string = True
                self.key = bool(self.stack and self.stack[-1]['kind'] == '{' and self.stack[-1]['expect'] == 'key')
                self.token, self.target = [], None
                if not self.key:
                    path = self.value_path()
                    if path == ('answer',) or (len(path) == 3 and path[0] == 'answer_units'
                                               and isinstance(path[1], int) and path[2] == 'text'):
                        self.target = path
                        self.parts[path] = []
            elif char in '{[':
                path = self.value_path()
                if path == ('answer_units',) and char == '[':
                    self.units = True
                self.stack.append({'kind': char, 'path': path, 'expect': 'key' if char == '{' else 'value',
                                   'key': None, 'keys': set(), 'index': 0})
            elif char in '}]':
                if not self.stack or self.stack[-1]['kind'] != ('{' if char == '}' else '['):
                    self.invalid = True
                else:
                    self.stack.pop()
                    self.complete()
            elif char == ':' and self.stack:
                self.stack[-1]['expect'] = 'value'
            elif char == ',' and self.stack:
                frame = self.stack[-1]
                frame['index'] += 1
                frame['expect'] = 'key' if frame['kind'] == '{' else 'value'
            else:
                self.primitive = True

    def text(self):
        if self.invalid:
            return ''
        if self.units:
            return '\n\n'.join(''.join(value).strip() for path, value in self.parts.items()
                               if len(path) == 3 and path[0] == 'answer_units')
        return ''.join(self.parts.get(('answer',), [])).strip()


class DraftStreams:
    """Bounded number of volatile previews; response/token budgets are unchanged."""
    def __init__(self):
        self.lock, self.states = threading.RLock(), {}

    def begin(self, rid, supported=True):
        with self.lock:
            revision = self.states.get(rid, {}).get('revision', 0) + 1
            self.states[rid] = dict(revision=revision, parser=AnswerPreview(), reasoning=[],
                                    stage='generating' if supported else 'waiting', closed=False)
            for key in list(self.states):
                if len(self.states) <= 64:
                    break
                if key != rid and self.states[key]['closed']:
                    del self.states[key]

    def feed(self, rid, kind, text):
        if not isinstance(text, str) or not text:
            return
        with self.lock:
            state = self.states[rid]
            if kind == 'content':
                state['parser'].feed(text)
            elif kind == 'reasoning':
                state['reasoning'].append(text)
            state['revision'] += 1

    def finish(self, rid):
        with self.lock:
            if rid in self.states:
                self.states[rid]['stage'] = 'checking'
                self.states[rid]['closed'] = True
                self.states[rid]['revision'] += 1

    def snapshot(self, rid):
        with self.lock:
            state = self.states.get(rid)
            if not state:
                return dict(revision=0, text='', reasoning='', stage='waiting')
            return dict(revision=state['revision'], text=state['parser'].text(),
                        reasoning=''.join(state['reasoning']), stage=state['stage'])


def provider_stream(response, protocol, client, emit):
    """Collect original JSON bytes while forwarding actual provider deltas."""
    from .llm import LLMError, OutputLimitError
    body, reasoning, terminal, stop = [], [], False, None
    for payload in sse_data(response):
        if payload == '[DONE]' and protocol == 'openai':
            terminal = True
            break
        data = json.loads(payload)
        if data.get('error') or data.get('type') == 'error':
            raise LLMError('流式服务返回错误，未发布不完整回复。')
        text, thought = '', ''
        if protocol == 'openai':
            if isinstance(data.get('usage'), dict):
                client.last_usage = data['usage']
            choices = [c for c in data.get('choices', []) if c.get('index', 0) == 0]
            if choices:
                choice = choices[0]
                stop = choice.get('finish_reason') or stop
                delta = choice.get('delta') or {}
                text = delta.get('content') or ''
                thought = delta.get('reasoning_content') or delta.get('reasoning') or ''
        else:
            kind = data.get('type')
            if kind == 'message_start':
                usage = data.get('message', {}).get('usage')
                client.last_usage = dict(usage) if isinstance(usage, dict) else None
            elif kind == 'message_delta':
                stop = data.get('delta', {}).get('stop_reason') or stop
                usage = data.get('usage')
                if isinstance(usage, dict):
                    client.last_usage = {**(client.last_usage or {}), **usage}
            elif kind == 'message_stop':
                terminal = True
                break
            elif kind == 'content_block_start':
                block = data.get('content_block', {})
                text = block.get('text', '') if block.get('type') == 'text' else ''
                thought = block.get('thinking', '') if block.get('type') == 'thinking' else ''
            elif kind == 'content_block_delta':
                delta = data.get('delta', {})
                text = delta.get('text', '') if delta.get('type') == 'text_delta' else ''
                thought = delta.get('thinking', '') if delta.get('type') == 'thinking_delta' else ''
        if not isinstance(text, str) or not isinstance(thought, str):
            raise LLMError('流式响应类型错误，未发布回复。')
        if text:
            body.append(text)
            emit('content', text)
        if thought:
            reasoning.append(thought)
            emit('reasoning', thought)
    client.last_reasoning = ''.join(reasoning)
    if stop in ('length', 'max_tokens'):
        raise OutputLimitError('模型输出达到上限，流式回复不完整；用量和待审状态保留。')
    if not terminal or stop not in ('stop', 'end_turn', 'stop_sequence'):
        raise LLMError('流式回复未正常结束，结果和费用可能未知；不会自动重发。')
    return ''.join(body)

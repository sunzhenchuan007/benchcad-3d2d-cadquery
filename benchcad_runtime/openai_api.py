"""Stateless Responses transport. Credentials are never passed to CAD executors."""
from pathlib import Path
import base64
import json
import math
import os
import subprocess
import sys
import time
from urllib.parse import urlsplit
from .episode import Reply, TransportError
from .local_accounting import estimate_request, account_response, validate_instructions

MAX_REQUEST_BYTES = 64 * 1024 * 1024


class RequestBudgetError(RuntimeError):
    """Controller request budget, not a model decision or retryable API error."""
    def __init__(self, limit, phase):
        super().__init__('Framework request byte budget exceeded; history was not truncated')
        self.local_usage = None
        self.diagnostic = {'failure_kind': 'request_size_limit',
                           'attribution': 'framework_request_budget',
                           'request_sent': False, 'limit_bytes': limit, 'phase': phase}


def preflight_request_bytes(metadata, messages, limit):
    """Predict HTTP JSON bytes using PNG file sizes, without reading image data.

    JSON settings match api_worker.py. Escaped Unicode/quotes are included, so
    UTF-8 text and JSON framing cannot evade the request-size check.
    """
    inputs, encoded_image_bytes = [], 0
    for msg in messages:
        if msg['role'] == 'assistant':
            inputs.append({'role': 'assistant', 'content': msg['text']})
            continue
        content = [{'type': 'input_text', 'text': msg['text']}]
        for filename in msg.get('images', []):
            size = Path(filename).stat().st_size
            if size > 12 * 1024 * 1024:
                raise ValueError('Observation exceeds 12 MiB')
            encoded_image_bytes += 4 * ((size + 2) // 3)
            if encoded_image_bytes > limit:
                raise RequestBudgetError(limit, 'preflight')
            content.append({'type': 'input_image', 'detail': 'high',
                            'image_url': 'data:image/png;base64,'})
        inputs.append({'role': 'user', 'content': content})
    estimated = encoded_image_bytes
    # iterencode avoids one additional full text-payload copy during preflight.
    for chunk in json.JSONEncoder().iterencode(dict(metadata, input=inputs)):
        estimated += len(chunk.encode('utf-8'))
        if estimated > limit:
            raise RequestBudgetError(limit, 'preflight')
    return estimated


def safe_http_diagnostic(value):
    """Preserve HTTP attribution without copying arbitrary provider data."""
    if not isinstance(value, dict):
        return None
    status = value.get('http_status')
    if value.get('failure_kind') == 'http_error' and type(status) is int and 100 <= status <= 599:
        return {'failure_kind': 'http_error', 'http_status': status,
                'attribution': 'transport_or_service'}
    return None


def encode_messages(messages):
    result = []
    for msg in messages:
        if msg['role'] == 'assistant':
            result.append({'role':'assistant', 'content':msg['text']})
            continue
        content = [{'type':'input_text', 'text':msg['text']}]
        for filename in msg.get('images', []):
            p = Path(filename)
            if p.stat().st_size > 12*1024*1024:
                raise ValueError('Observation exceeds 12 MiB')
            data = p.read_bytes()
            if not data.startswith(b'\x89PNG\r\n\x1a\n'):
                raise ValueError('Only validated PNG observations are accepted')
            content.append({'type':'input_image','detail':'high',
                            'image_url':'data:image/png;base64,'+base64.b64encode(data).decode('ascii')})
        result.append({'role':'user','content':content})
    return result


def responses_url(base_url):
    url = urlsplit(base_url)
    if url.username or url.password or url.query or url.fragment or not url.hostname:
        raise ValueError('Base URL must not contain credentials, query or fragment')
    if url.scheme != 'https' and not (url.scheme=='http' and url.hostname in ('localhost','127.0.0.1','::1')):
        raise ValueError('Use HTTPS, or HTTP only for a loopback API service')
    return base_url.rstrip('/')+'/responses'


class OpenAIResponses:
    def __init__(self, model, max_output_tokens=8192, reasoning_effort=None, base_url=None,
                 max_request_bytes=MAX_REQUEST_BYTES):
        if not model or not model.strip():
            raise ValueError('An exact model ID is required; no automatic model substitution')
        if not os.environ.get('OPENAI_API_KEY'):
            raise ValueError('Set OPENAI_API_KEY in the controller environment')
        if type(max_output_tokens) is not int or max_output_tokens <= 0:
            raise ValueError('max_output_tokens must be a positive integer')
        if type(max_request_bytes) is not int or max_request_bytes <= 0:
            raise ValueError('max_request_bytes must be a positive integer')
        self.max_request_bytes = max_request_bytes
        self.model, self.max_output_tokens = model, max_output_tokens
        self.reasoning_effort = reasoning_effort
        self.url = responses_url(base_url or os.environ.get('OPENAI_BASE_URL','https://api.openai.com/v1'))

    def __call__(self, system, messages, timeout):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('Request timeout must be finite and positive')
        deadline = time.monotonic() + timeout

        def remaining():
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise TransportError('Request preparation exhausted stage deadline; no API request sent',
                                     diagnostic={'failure_kind': 'stage_request_deadline',
                                                 'attribution': 'framework_request_preparation',
                                                 'request_sent': False})
            return seconds

        validate_instructions(system)
        payload = {'model':self.model, 'instructions':system,
                   'max_output_tokens':self.max_output_tokens, 'store':False}
        if self.reasoning_effort:
            payload['reasoning'] = {'effort':self.reasoning_effort}
        preflight_request_bytes(payload, messages, self.max_request_bytes)
        remaining()
        payload['input'] = encode_messages(messages)
        remaining()
        local_usage = estimate_request(payload)
        remaining()
        request_json = json.dumps(payload)
        if len(request_json.encode('utf-8')) > self.max_request_bytes:
            raise RequestBudgetError(self.max_request_bytes, 'serialized')
        # Embed the already serialized request instead of serializing it again.
        # The host timeout below includes preparation of this final IPC envelope.
        socket_timeout = remaining()
        worker_input = ('{"url":' + json.dumps(self.url) + ',"request":' + request_json
                        + ',"timeout_seconds":' + json.dumps(socket_timeout) + '}')
        process_timeout = remaining()
        # The worker is controller-side only. Killing it enforces an overall
        # request deadline, unlike an individual socket read timeout.
        try:
            proc = subprocess.run([sys.executable, str(Path(__file__).with_name('api_worker.py'))],
                                  input=worker_input, text=True, encoding='utf-8',
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  timeout=process_timeout,
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        except subprocess.TimeoutExpired as exc:
            raise TransportError('OpenAI request deadline exceeded; output usage unknown', local_usage,
                                 {'failure_kind': 'stage_request_deadline', 'attribution': 'budget_or_transport'}) from exc
        except (OSError, UnicodeError) as exc:
            raise TransportError('OpenAI worker launch failed; output usage unknown', local_usage) from exc
        if proc.returncode:
            raise TransportError('OpenAI transport worker failed; details suppressed', local_usage)
        try:
            response = json.loads(proc.stdout)
            if not isinstance(response, dict):
                raise ValueError("Response must be an object")
            if response.get('transport_error'):
                raise TransportError(response['transport_error'], local_usage, response.get('diagnostic'))
            if response.get('fatal_error'):
                raise ModelConfigurationError(response['fatal_error'], local_usage,
                                              safe_http_diagnostic(response.get('diagnostic')))
            if not isinstance(response.get('output'), list):
                raise ValueError("Missing response output")
            parts = []
            refused = False
            for item in response['output']:
                if item.get('type') != 'message':
                    continue  # Reasoning and other non-message output are not visible text.
                for part in item['content']:
                    if part.get('type') == 'output_text':
                        parts.append(part['text'])
                    elif part.get('type') == 'refusal':
                        parts.append(part['refusal'])
                        refused = True
            if any(not isinstance(part, str) for part in parts):
                raise ValueError("Invalid visible text")
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise TransportError('OpenAI response malformed; output usage unknown', local_usage) from exc
        local_usage = account_response(local_usage, parts, response.get('model'))
        # Preserve all returned visible text, but never execute incomplete/refused replies.
        return Reply('\n'.join(parts), response.get('usage'), local_usage,
                     executable=response.get('status') == 'completed' and not refused,
                     response_status=response.get('status'))


class ModelConfigurationError(RuntimeError):
    def __init__(self, message, local_usage=None, diagnostic=None):
        super().__init__(message)
        self.local_usage = local_usage
        self.diagnostic = diagnostic

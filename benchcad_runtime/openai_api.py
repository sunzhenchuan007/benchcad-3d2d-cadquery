"""Stateless Responses transport. Credentials are never passed to CAD executors."""
from pathlib import Path
import base64
import json
import os
import subprocess
import sys
from urllib.parse import urlsplit
from .episode import Reply, TransportError
from .local_accounting import estimate_request, account_response, validate_instructions


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
    def __init__(self, model, max_output_tokens=8192, reasoning_effort=None, base_url=None):
        if not model or not model.strip():
            raise ValueError('An exact model ID is required; no automatic model substitution')
        if not os.environ.get('OPENAI_API_KEY'):
            raise ValueError('Set OPENAI_API_KEY in the controller environment')
        if type(max_output_tokens) is not int or max_output_tokens <= 0:
            raise ValueError('max_output_tokens must be a positive integer')
        self.model, self.max_output_tokens = model, max_output_tokens
        self.reasoning_effort = reasoning_effort
        self.url = responses_url(base_url or os.environ.get('OPENAI_BASE_URL','https://api.openai.com/v1'))

    def __call__(self, system, messages, timeout):
        validate_instructions(system)
        payload = {'model':self.model, 'instructions':system, 'input':encode_messages(messages),
                   'max_output_tokens':self.max_output_tokens, 'store':False}
        if self.reasoning_effort:
            payload['reasoning'] = {'effort':self.reasoning_effort}
        local_usage = estimate_request(payload)
        # The worker is controller-side only. Killing it enforces an overall
        # request deadline, unlike an individual socket read timeout.
        try:
            proc = subprocess.run([sys.executable, str(Path(__file__).with_name('api_worker.py'))],
                                  input=json.dumps({'url':self.url,'request':payload,'timeout_seconds':timeout}), text=True, encoding='utf-8',
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  timeout=timeout,
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        except subprocess.TimeoutExpired as exc:
            raise TransportError('OpenAI request deadline exceeded; output usage unknown', local_usage) from exc
        except (OSError, UnicodeError) as exc:
            raise TransportError('OpenAI worker launch failed; output usage unknown', local_usage) from exc
        if proc.returncode:
            raise TransportError('OpenAI transport worker failed; details suppressed', local_usage)
        try:
            response = json.loads(proc.stdout)
            if not isinstance(response, dict):
                raise ValueError("Response must be an object")
            if response.get('transport_error'):
                raise TransportError(response['transport_error'], local_usage)
            if response.get('fatal_error'):
                raise ModelConfigurationError(response['fatal_error'], local_usage)
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
    def __init__(self, message, local_usage=None):
        super().__init__(message)
        self.local_usage = local_usage

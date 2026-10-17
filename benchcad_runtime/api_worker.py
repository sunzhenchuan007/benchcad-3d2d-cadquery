"""Controller-only HTTP worker; emit no request, credential or raw error body."""
import json
import math
import os
import socket
import ssl
import sys
import urllib.error
import urllib.request


def main():
    payload = json.load(sys.stdin)
    # The supervising process enforces the remaining stage wall budget. A
    # separate short socket timeout used to discard valid slow reasoning calls.
    request_timeout = float(payload.get('timeout_seconds', 120))
    if not math.isfinite(request_timeout) or request_timeout <= 0:
        raise ValueError('Request timeout must be finite and positive')
    req = urllib.request.Request(payload['url'],
                                 data=json.dumps(payload['request']).encode('utf-8'),
                                 headers={'Authorization':'Bearer '+os.environ['OPENAI_API_KEY'],
                                          'Content-Type':'application/json'})
    try:
        # Do not forward the bearer token through redirects to another host.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=request_timeout) as stream:
            raw = stream.read(32*1024*1024+1)
        if len(raw)>32*1024*1024:
            raise ValueError('oversize response')
        response = json.loads(raw)
        # Retain only fields consumed by the caller. No response account IDs.
        result = {k:response.get(k) for k in ('status','output','usage','model')}
    except urllib.error.HTTPError as exc:
        kind = 'transport_error' if exc.code in (408,429) or exc.code>=500 else 'fatal_error'
        result = {kind:f'API HTTP {exc.code}; request did not produce a usable decision',
                  'diagnostic': {'failure_kind': 'http_error', 'http_status': exc.code,
                                 'attribution': 'transport_or_service'}}
    except Exception as exc:
        cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        if isinstance(cause, (TimeoutError, socket.timeout)):
            failure = 'socket_timeout'
        elif isinstance(cause, socket.gaierror):
            failure = 'dns_error'
        elif isinstance(cause, ssl.SSLError):
            failure = 'tls_error'
        elif isinstance(cause, json.JSONDecodeError):
            failure = 'response_json_error'
        elif isinstance(cause, ValueError) and str(cause) == 'oversize response':
            failure = 'response_size_limit'
        elif isinstance(cause, (TypeError, AttributeError)):
            failure = 'response_schema_error'
        else:
            failure = 'network_or_worker_error'
        result = {'transport_error':f'API failure: {failure}; sensitive details suppressed',
                  'diagnostic': {'failure_kind': failure, 'error_type': type(cause).__name__,
                                 'attribution': 'transport_or_protocol'}}
    sys.stdout.write(json.dumps(result))


if __name__=='__main__':
    main()

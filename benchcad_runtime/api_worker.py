"""Controller-only HTTP worker; emit no request, credential or raw error body."""
import json
import math
import os
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
        result = {kind:f'OpenAI HTTP {exc.code}; check account/model configuration'}
    except Exception:
        result = {'transport_error':'OpenAI network or response failure; details suppressed'}
    sys.stdout.write(json.dumps(result))


if __name__=='__main__':
    main()

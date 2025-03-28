import hmac
import json

from django.shortcuts import get_object_or_404, render
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt

from .models import CheckIn, Computer


X_LMIO_AUTH = "X-lmio-auth"


def _make_response(status_code, body, hmac_key):
    resp = HttpResponse(body, status=status_code)
    resp.headers[X_LMIO_AUTH] = hmac.HMAC(hmac_key, body.encode("utf-8"), "sha1").hexdigest()
    return resp


@csrf_exempt
def ping(request):
    auth = request.headers.get(X_LMIO_AUTH)

    key = "abcde".encode("utf-8")
    req_digest = hmac.HMAC(key, request.body, "sha1").hexdigest()

    body = json.loads(request.body)
    print(body)

    is_good = hmac.compare_digest(auth, req_digest)

    if is_good:
        resp_body = json.dumps({"timestamp": body["timestamp"], "status": "200", "message": "OK"})
        status = 200
    else:
        resp_body = "Invalid authentication data"
        status = 403

    return _make_response(status, resp_body, key)

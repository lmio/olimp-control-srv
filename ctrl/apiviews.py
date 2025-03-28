import hmac

from django.shortcuts import get_object_or_404, render
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt

from .models import CheckIn, Computer


X_LMIO_AUTH = "X-lmio-auth"

@csrf_exempt
def ping(request):
    auth = request.headers.get(X_LMIO_AUTH)
    body = request.body

    key = "abcde"
    req_digest = hmac.HMAC(key.encode("utf-8"), body, "sha1").digest()

    auth_bytes = bytes.fromhex(auth)

    is_good = auth_bytes == req_digest

    return HttpResponse(f"{is_good}\n{auth_bytes}\n{req_digest}")

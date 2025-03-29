import hmac
import json

from django.http import HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .models import CheckIn, Computer, UnknownComputer


X_LMIO_AUTH = "X-lmio-auth"
AUTH_KEY = "abcde".encode("utf-8")


def _make_response(status_code, body, hmac_key=AUTH_KEY):
    resp = HttpResponse(body, status=status_code)
    resp.headers[X_LMIO_AUTH] = hmac.HMAC(hmac_key, body.encode("utf-8"), "sha1").hexdigest()
    return resp


def _process_request_basics(request):
    req_digest = hmac.HMAC(AUTH_KEY, request.body, "sha1").hexdigest()
    auth = request.headers.get(X_LMIO_AUTH)
    is_good = hmac.compare_digest(auth, req_digest)

    if not is_good:
        resp_body = "Invalid authentication data"
        status = 403
        return _make_response(status, resp_body)
    
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError as e:
        resp_body = f"Invalid JSON data: {e}"
        status = 400
        return _make_response(status, resp_body)

    mid = body["mid"]
    try:
        comp = Computer.objects.get(machine_id=mid)
    except Computer.DoesNotExist:
        uc, created = UnknownComputer.objects.get_or_create(machine_id=mid)
        if not created:
            uc.save()
        resp_body = f"Computer with machine_id {mid} is not registered"
        status = 404
        return _make_response(status, resp_body)

    return (body, comp)


def _process_next_ticket(body, comp):
    next_ticket = comp.ticket_set.filter(completed=None).order_by("added")[:1]
    if next_ticket:
        next_ticket = next_ticket[0]
        if not next_ticket.fetched:
            next_ticket.fetched = timezone.now()
            next_ticket.save()

        resp_body = json.dumps({
            "timestamp": body["timestamp"],
            "status": 200,
            "message": f"Ticket {next_ticket.pk}",
            "tid": next_ticket.pk,
            "cmd": next_ticket.task.payload,
            "runAs": next_ticket.task.run_as,
        })
        status = 200
        return _make_response(status, resp_body)

    else:
        resp_body = json.dumps({
            "timestamp": body["timestamp"],
            "status": 404,
            "message": "No tickets available",
        })
        status = 404
        return _make_response(status, resp_body)


def _process_ticket_results(body, comp):
    tid = body.get("tid")
    if not tid:
        resp_body = "No ticket id provided"
        status = 400
        return _make_response(status, resp_body)

    next_ticket = comp.ticket_set.filter(completed=None).order_by("added")[:1]
    if not next_ticket:
        resp_body = "Posting results but no tickets available"
        status = 400
        return _make_response(status, resp_body)

    next_ticket = next_ticket[0]
    if next_ticket.pk != tid:
        resp_body = "Posting results for unexpected ticket"
        status = 400
        return _make_response(status, resp_body)

    if not next_ticket.fetched:
        resp_body = "Posting results for ticket not fetched"
        status = 400
        return _make_response(status, resp_body)

    next_ticket.completed = timezone.now()
    next_ticket.runtime = body.get("exectime")
    next_ticket.exit_code = body.get("exitcode")
    next_ticket.stdout = body.get("stdout")
    next_ticket.stderr = body.get("stderr")
    next_ticket.save()

    resp_body = json.dumps({
        "timestamp": body["timestamp"],
        "status": 200,
        "message": "Results accepted",
    })
    status = 200
    return _make_response(status, resp_body)


@csrf_exempt
def ping(request):
    data = _process_request_basics(request)
    if isinstance(data, HttpResponse):
        return data
    body, comp = data

    checkin = CheckIn(computer=comp,
                      pseudo_timestamp=body["timestamp"],
                      uptime=body["uptime"],
                      has_root=body["hasRoot"])
    checkin.save()

    resp_body = json.dumps({
        "timestamp": body["timestamp"],
        "status": 200,
        "message": "OK",
    })
    status = 200
    return _make_response(status, resp_body)


@csrf_exempt
def ticket(request):
    data = _process_request_basics(request)
    if isinstance(data, HttpResponse):
        return data
    body, comp = data

    if request.method == "GET":
        return _process_next_ticket(body, comp)
    elif request.method == "POST":
        return _process_ticket_results(body, comp)
    else:
        resp_body = "Invalid request method"
        status = 400
        return _make_response(status, resp_body)

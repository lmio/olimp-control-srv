"""Read-only service API consumed by the separate toilet application.

Olimp-control owns contestants, computers, class membership, and physical
layout. Toilet operator accounts intentionally do not cross this boundary:
the toilet application owns and authenticates its own proctors.
"""

import hashlib
import hmac
import json
import time

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .models import Computer, Location, Student


TIMESTAMP_HEADER = "X-LMIO-Toilet-Timestamp"
AUTH_HEADER = "X-LMIO-Toilet-Auth"


def _auth_key():
    return settings.CTRL_TOILET_AUTH_KEY.encode("utf-8")


def request_signature(timestamp, method, full_path, body):
    canonical = (
        timestamp.encode("utf-8")
        + b"\n"
        + method.upper().encode("ascii")
        + b"\n"
        + full_path.encode("utf-8")
        + b"\n"
        + body
    )
    return hmac.new(_auth_key(), canonical, hashlib.sha256).hexdigest()


def response_signature(timestamp, status_code, body):
    canonical = (
        timestamp.encode("utf-8")
        + b"\n"
        + str(status_code).encode("ascii")
        + b"\n"
        + body
    )
    return hmac.new(_auth_key(), canonical, hashlib.sha256).hexdigest()


def _signed_json(request, payload, status=200):
    response = JsonResponse(
        payload,
        status=status,
        json_dumps_params={"sort_keys": True, "separators": (",", ":")},
    )
    request_timestamp = request.headers.get(TIMESTAMP_HEADER, "")
    response.headers[AUTH_HEADER] = response_signature(
        request_timestamp,
        status,
        response.content,
    )
    response.headers[TIMESTAMP_HEADER] = request_timestamp
    response.headers["Cache-Control"] = "no-store"
    return response


def _service_auth_failure(request):
    timestamp = request.headers.get(TIMESTAMP_HEADER, "")
    supplied_signature = request.headers.get(AUTH_HEADER, "")
    try:
        parsed_timestamp = int(timestamp)
    except (TypeError, ValueError):
        return _signed_json(request, {"error": "invalid_service_auth"}, status=403)

    if abs(int(time.time()) - parsed_timestamp) > settings.CTRL_TOILET_AUTH_MAX_SKEW_SECONDS:
        return _signed_json(request, {"error": "invalid_service_auth"}, status=403)

    expected_signature = request_signature(
        timestamp,
        request.method,
        request.get_full_path(),
        request.body,
    )
    if not hmac.compare_digest(supplied_signature, expected_signature):
        return _signed_json(request, {"error": "invalid_service_auth"}, status=403)
    return None


def _require_method(request, method):
    if request.method == method:
        return None
    response = _signed_json(request, {"error": "method_not_allowed"}, status=405)
    response.headers["Allow"] = method
    return response


def _json_body(request):
    try:
        payload = json.loads(request.body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _class_json(location):
    return {
        "id": str(location.public_id),
        "name": location.name,
        "sequence_num": location.sequence_num,
        "grid_cols": location.grid_cols,
    }


def _student_json(student):
    return {
        "id": student.pk,
        "userid": student.userid,
    }


def _computer_json(computer, *, include_class=False):
    contestant = computer.contestant
    payload = {
        "machine_id": computer.machine_id,
        "name": computer.name,
        "sequence_num": computer.sequence_num,
        "grid_row": computer.grid_row,
        "grid_col": computer.grid_col,
        "student": _student_json(contestant) if contestant else None,
    }
    if include_class:
        payload["class"] = (
            _class_json(computer.location) if computer.location is not None else None
        )
    return payload


def _ordered_computers():
    return Computer.objects.select_related(
        "location", "contestant_assignment__contestant"
    ).order_by(
        "location__sequence_num",
        "location__name",
        "sequence_num",
        "name",
        "machine_id",
    )


@csrf_exempt
def classes(request):
    failure = _service_auth_failure(request)
    if failure is not None:
        return failure
    failure = _require_method(request, "GET")
    if failure is not None:
        return failure

    catalog = [
        _class_json(location)
        for location in Location.objects.order_by(
            "sequence_num", "name", "public_id"
        )
    ]
    return _signed_json(request, {"classes": catalog})


@csrf_exempt
def class_layout(request, public_id):
    failure = _service_auth_failure(request)
    if failure is not None:
        return failure
    failure = _require_method(request, "GET")
    if failure is not None:
        return failure

    location = Location.objects.filter(public_id=public_id).first()
    if location is None:
        return _signed_json(request, {"error": "class_not_found"}, status=404)
    computers = (
        Computer.objects.select_related("contestant_assignment__contestant")
        .filter(location=location)
        .order_by("sequence_num", "name", "machine_id")
    )
    return _signed_json(
        request,
        {
            "class": _class_json(location),
            "computers": [_computer_json(computer) for computer in computers],
        },
    )


@csrf_exempt
def students(request):
    failure = _service_auth_failure(request)
    if failure is not None:
        return failure
    failure = _require_method(request, "GET")
    if failure is not None:
        return failure

    roster = [
        _student_json(student)
        for student in Student.objects.order_by("userid", "pk")
    ]
    return _signed_json(request, {"students": roster})


def student_assignment_payload(userid):
    try:
        student = Student.objects.select_related(
            "computer_assignment__computer__location",
            "computer_assignment__computer__contestant_assignment__contestant",
        ).get(userid=userid)
    except Student.DoesNotExist:
        return {
            "found": False,
            "userid": userid,
            "student": None,
            "computers": [],
            "classes": [],
            "anomalies": [{"code": "student_not_found"}],
        }

    assigned_computers = [student.computer] if student.computer else []
    locations = {}
    unassigned_machine_ids = []
    for computer in assigned_computers:
        if computer.location is None:
            unassigned_machine_ids.append(computer.machine_id)
        else:
            locations[computer.location.public_id] = computer.location

    ordered_locations = sorted(
        locations.values(),
        key=lambda location: (location.sequence_num, location.name, location.public_id),
    )
    anomalies = []
    if not assigned_computers:
        anomalies.append({"code": "no_computers"})
    if unassigned_machine_ids:
        anomalies.append(
            {
                "code": "computer_without_class",
                "machine_ids": sorted(unassigned_machine_ids),
            }
        )
    return {
        "found": True,
        "userid": student.userid,
        "student": _student_json(student),
        "computers": [
            _computer_json(computer, include_class=True)
            for computer in assigned_computers
        ],
        "classes": [_class_json(location) for location in ordered_locations],
        "anomalies": anomalies,
    }


@csrf_exempt
def student_assignment(request):
    failure = _service_auth_failure(request)
    if failure is not None:
        return failure
    failure = _require_method(request, "POST")
    if failure is not None:
        return failure

    payload = _json_body(request)
    userid = payload.get("userid") if payload is not None else None
    if not isinstance(userid, str) or not userid.strip():
        return _signed_json(request, {"error": "invalid_request"}, status=400)
    return _signed_json(request, student_assignment_payload(userid.strip()))

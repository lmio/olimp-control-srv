"""Authenticated JSON management API for Olimp-control source-of-truth data.

This surface is intended for trusted server-side management clients.  It uses
HTTP Basic authentication so callers can rely on Django's normal password
backend and permission model without sharing the toilet service HMAC key with a
browser.  Credentials must only be sent over HTTPS or an equivalently private
transport.
"""

from __future__ import annotations

import base64
import binascii
from functools import wraps
import json
import uuid

from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .assignments import (
    AssignmentConflict,
    assign_contestant_to_computer,
    remove_contestant_computer_assignment,
)
from .models import (
    Computer,
    ContestantComputerAssignment,
    Location,
    Student,
)


MAX_JSON_BODY_BYTES = 64 * 1024
BASIC_CHALLENGE = 'Basic realm="Olimp-control management", charset="UTF-8"'


class ManagementAPIError(Exception):
    def __init__(self, status, code, *, field=None, detail=None, headers=None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.field = field
        self.detail = detail
        self.headers = headers or {}


def _json_response(payload, *, status=200, headers=None):
    response = JsonResponse(
        payload,
        status=status,
        json_dumps_params={"sort_keys": True, "separators": (",", ":")},
    )
    for name, value in (headers or {}).items():
        response.headers[name] = value
    response.headers["Cache-Control"] = "no-store"
    return response


def _error_response(error):
    payload = {"error": error.code}
    if error.field is not None:
        payload["field"] = error.field
    if error.detail is not None:
        payload["detail"] = error.detail
    return _json_response(payload, status=error.status, headers=error.headers)


def _no_content_response():
    response = HttpResponse(status=204)
    response.headers["Cache-Control"] = "no-store"
    return response


def _basic_user(request):
    authorization = request.headers.get("Authorization", "")
    scheme, separator, encoded = authorization.partition(" ")
    if not separator or scheme.lower() != "basic" or not encoded or any(
        character.isspace() for character in encoded
    ):
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    username, separator, password = decoded.partition(":")
    if not separator or not username:
        return None
    user = authenticate(request=request, username=username, password=password)
    if user is None or not user.is_active:
        return None
    return user


def management_endpoint(view):
    """Authenticate a management request and return JSON for every failure."""

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        user = _basic_user(request)
        if user is None:
            return _error_response(
                ManagementAPIError(
                    401,
                    "authentication_required",
                    headers={"WWW-Authenticate": BASIC_CHALLENGE},
                )
            )
        if not user.is_staff:
            return _error_response(ManagementAPIError(403, "forbidden"))
        request.management_user = user
        try:
            return view(request, *args, **kwargs)
        except ManagementAPIError as error:
            return _error_response(error)
        except ValidationError as error:
            if hasattr(error, "message_dict"):
                detail = error.message_dict
            else:
                detail = error.messages
            return _error_response(
                ManagementAPIError(400, "validation_error", detail=detail)
            )
        except IntegrityError:
            return _error_response(ManagementAPIError(409, "conflict"))

    return csrf_exempt(wrapped)


def _require_method(request, *methods):
    if request.method not in methods:
        raise ManagementAPIError(
            405,
            "method_not_allowed",
            headers={"Allow": ", ".join(methods)},
        )


def _require_permissions(user, *permissions):
    if not user.has_perms(permissions):
        raise ManagementAPIError(403, "forbidden")


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_non_finite(value):
    raise ValueError(f"non-finite JSON number: {value}")


def _strict_json_object(request, *, fields, required):
    if request.content_type != "application/json":
        raise ManagementAPIError(415, "unsupported_media_type")
    if len(request.body) > MAX_JSON_BODY_BYTES:
        raise ManagementAPIError(413, "request_too_large")
    try:
        text = request.body.decode("utf-8")
        payload = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ManagementAPIError(400, "invalid_json")
    if not isinstance(payload, dict):
        raise ManagementAPIError(400, "invalid_json", detail="expected an object")

    unknown = sorted(set(payload) - set(fields))
    if unknown:
        raise ManagementAPIError(
            400,
            "unknown_fields",
            detail=unknown,
        )
    missing = sorted(set(required) - set(payload))
    if missing:
        raise ManagementAPIError(
            400,
            "missing_fields",
            detail=missing,
        )
    return payload


def _string(payload, field, *, allow_empty=False, max_length=None, strip=True):
    value = payload.get(field)
    if not isinstance(value, str):
        raise ManagementAPIError(400, "invalid_field", field=field)
    normalized = value.strip() if strip else value
    if not allow_empty and not normalized:
        raise ManagementAPIError(400, "invalid_field", field=field)
    if max_length is not None and len(normalized) > max_length:
        raise ManagementAPIError(400, "invalid_field", field=field)
    return normalized


def _integer(payload, field):
    value = payload.get(field)
    if type(value) is not int:
        raise ManagementAPIError(400, "invalid_field", field=field)
    return value


def _nullable_positive_integer(payload, field):
    value = payload.get(field)
    if value is None:
        return None
    if type(value) is not int or value < 1:
        raise ManagementAPIError(400, "invalid_field", field=field)
    return value


def _boolean(payload, field):
    value = payload.get(field)
    if type(value) is not bool:
        raise ManagementAPIError(400, "invalid_field", field=field)
    return value


def _identifier(value, field, *, max_length):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ManagementAPIError(400, "invalid_identifier", field=field)
    if len(value) > max_length:
        raise ManagementAPIError(400, "invalid_identifier", field=field)
    return value


def _uuid(value, field):
    if not isinstance(value, str):
        raise ManagementAPIError(400, "invalid_field", field=field)
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        raise ManagementAPIError(400, "invalid_field", field=field)


def _unique_string_list(payload, field, *, max_length=None):
    values = payload.get(field)
    if not isinstance(values, list):
        raise ManagementAPIError(400, "invalid_field", field=field)
    normalized = []
    for value in values:
        if not isinstance(value, str) or not value or value != value.strip():
            raise ManagementAPIError(400, "invalid_field", field=field)
        if max_length is not None and len(value) > max_length:
            raise ManagementAPIError(400, "invalid_field", field=field)
        normalized.append(value)
    if len(normalized) != len(set(normalized)):
        raise ManagementAPIError(400, "duplicate_values", field=field)
    return normalized


def _unique_uuid_list(payload, field):
    raw_values = _unique_string_list(payload, field)
    values = [_uuid(value, field) for value in raw_values]
    if len(values) != len(set(values)):
        raise ManagementAPIError(400, "duplicate_values", field=field)
    return values


def _class_json(location):
    return {
        "id": str(location.public_id),
        "name": location.name,
        "sequence_num": location.sequence_num,
        "grid_cols": location.grid_cols,
    }


def _computer_json(computer):
    contestant = computer.contestant
    return {
        "machine_id": computer.machine_id,
        "name": computer.name,
        "class_id": str(computer.location.public_id) if computer.location else None,
        "student_userid": contestant.userid if contestant else None,
        "sequence_num": computer.sequence_num,
        "grid_row": computer.grid_row,
        "grid_col": computer.grid_col,
    }


def _student_json(student):
    computer = student.computer
    return {
        "id": student.pk,
        "userid": student.userid,
        "computer_id": computer.machine_id if computer else None,
        "class_id": (
            str(computer.location.public_id)
            if computer is not None and computer.location is not None
            else None
        ),
        "anomalies": student.assignment_anomalies,
    }


def _assignment_json(assignment):
    return {
        "contestant_identifier": assignment.contestant.userid,
        "computer_machine_id": assignment.computer.machine_id,
        "assigned_at": assignment.assigned_at.isoformat(),
        "source": assignment.source,
    }


def _admin_json(user):
    return {
        "username": user.get_username(),
        "first_name": user.first_name,
        "last_name": user.last_name,
        "email": user.email,
        "enabled": user.is_active,
        "is_staff": user.is_staff,
        "is_superuser": user.is_superuser,
    }


@management_endpoint
def state(request):
    _require_method(request, "GET")
    _require_permissions(
        request.management_user,
        "ctrl.view_location",
        "ctrl.view_computer",
        "ctrl.view_student",
        "ctrl.view_contestantcomputerassignment",
    )

    classes = Location.objects.order_by("sequence_num", "name", "public_id")
    computers = Computer.objects.select_related(
        "location", "contestant_assignment__contestant"
    ).order_by(
        "location__sequence_num",
        "location__name",
        "sequence_num",
        "name",
        "machine_id",
    )
    students = Student.objects.select_related(
        "computer_assignment__computer__location"
    ).order_by("userid")
    assignments = ContestantComputerAssignment.objects.select_related(
        "contestant", "computer"
    ).order_by("contestant__userid")
    return _json_response(
        {
            "classes": [_class_json(location) for location in classes],
            "computers": [_computer_json(computer) for computer in computers],
            "students": [_student_json(student) for student in students],
            "assignments": [
                _assignment_json(assignment) for assignment in assignments
            ],
        }
    )


@management_endpoint
def put_class(request, public_id):
    _require_method(request, "PUT", "DELETE")
    try:
        parsed_public_id = uuid.UUID(public_id)
    except (ValueError, AttributeError):
        raise ManagementAPIError(400, "invalid_identifier", field="id")
    if request.method == "DELETE":
        _require_permissions(
            request.management_user,
            "ctrl.delete_location",
            "ctrl.change_computer",
        )
        with transaction.atomic():
            location = (
                Location.objects.select_for_update()
                .filter(public_id=parsed_public_id)
                .first()
            )
            if location is not None:
                location.delete()
        return _no_content_response()

    payload = _strict_json_object(
        request,
        fields={"name", "sequence_num", "grid_cols"},
        required={"name", "sequence_num"},
    )
    name = _string(payload, "name")
    sequence_num = _integer(payload, "sequence_num")
    grid_cols = (
        _nullable_positive_integer(payload, "grid_cols")
        if "grid_cols" in payload
        else None
    )

    with transaction.atomic():
        location = (
            Location.objects.select_for_update()
            .filter(public_id=parsed_public_id)
            .first()
        )
        created = location is None
        required_permissions = [
            "ctrl.add_location" if created else "ctrl.change_location"
        ]
        if (
            not created
            and "grid_cols" in payload
            and grid_cols != location.grid_cols
        ):
            required_permissions.append("ctrl.change_computer")
        _require_permissions(request.management_user, *required_permissions)
        if created:
            location = Location(public_id=parsed_public_id)
        location.name = name
        location.sequence_num = sequence_num
        if "grid_cols" in payload:
            location.grid_cols = grid_cols
        location.full_clean()
        location.save()
    return _json_response(_class_json(location), status=201 if created else 200)


@management_endpoint
def put_computer(request, machine_id):
    _require_method(request, "PUT", "DELETE")
    machine_id = _identifier(machine_id, "machine_id", max_length=40)
    if request.method == "DELETE":
        _require_permissions(request.management_user, "ctrl.delete_computer")
        with transaction.atomic():
            computer = (
                Computer.objects.select_for_update(of=("self",))
                .select_related("contestant_assignment__contestant")
                .filter(machine_id=machine_id)
                .first()
            )
            if computer is not None:
                if computer.contestant is not None:
                    raise ManagementAPIError(
                        409,
                        "assignment_exists",
                        detail=(
                            f'Computer "{computer.machine_id}" is assigned to '
                            f'contestant "{computer.contestant.userid}". Remove '
                            "that mapping first."
                        ),
                    )
                computer.delete()
        return _no_content_response()

    payload = _strict_json_object(
        request,
        fields={"name", "class_id", "sequence_num"},
        required={"name", "class_id", "sequence_num"},
    )
    name = _string(payload, "name", max_length=32)
    sequence_num = _integer(payload, "sequence_num")
    class_id = payload["class_id"]
    if class_id is not None:
        class_id = _uuid(class_id, "class_id")
    with transaction.atomic():
        location = None
        if class_id is not None:
            location = (
                Location.objects.select_for_update()
                .filter(public_id=class_id)
                .first()
            )
            if location is None:
                raise ManagementAPIError(
                    400, "unknown_reference", field="class_id"
                )
        computer = (
            Computer.objects.select_for_update()
            .filter(machine_id=machine_id)
            .first()
        )
        created = computer is None
        _require_permissions(
            request.management_user,
            "ctrl.add_computer" if created else "ctrl.change_computer",
        )
        if created:
            computer = Computer(machine_id=machine_id)
        original_location_id = computer.location_id
        computer.name = name
        computer.location = location
        computer.sequence_num = sequence_num
        if not created and computer.location_id != original_location_id:
            computer.grid_row = None
            computer.grid_col = None
        computer.full_clean()
        computer.save()
    computer.location = location
    return _json_response(_computer_json(computer), status=201 if created else 200)


@management_endpoint
def put_student(request, userid):
    _require_method(request, "PUT", "DELETE")
    userid = _identifier(userid, "userid", max_length=64)
    if request.method == "DELETE":
        _require_permissions(request.management_user, "ctrl.delete_student")
        with transaction.atomic():
            student = (
                Student.objects.select_for_update(of=("self",))
                .select_related("computer_assignment__computer")
                .filter(userid=userid)
                .first()
            )
            if student is not None:
                if student.computer is not None:
                    raise ManagementAPIError(
                        409,
                        "assignment_exists",
                        detail=(
                            f'Contestant "{student.userid}" is assigned to '
                            f'computer "{student.computer.machine_id}". Remove '
                            "that mapping first."
                        ),
                    )
                student.delete()
        return _no_content_response()

    _strict_json_object(request, fields=set(), required=set())
    with transaction.atomic():
        student = (
            Student.objects.select_for_update()
            .filter(userid=userid)
            .first()
        )
        created = student is None
        _require_permissions(
            request.management_user,
            "ctrl.add_student" if created else "ctrl.change_student",
        )
        if created:
            student = Student(userid=userid)
        student.full_clean()
        student.save()
    student = Student.objects.select_related(
        "computer_assignment__computer__location"
    ).get(pk=student.pk)
    return _json_response(_student_json(student), status=201 if created else 200)


@management_endpoint
def put_assignment(request, userid):
    _require_method(request, "PUT", "DELETE")
    userid = _identifier(userid, "userid", max_length=64)
    if request.method == "DELETE":
        _require_permissions(
            request.management_user,
            "ctrl.delete_contestantcomputerassignment",
        )
        assignment = (
            ContestantComputerAssignment.objects.select_related(
                "contestant", "computer"
            )
            .filter(contestant__userid=userid)
            .first()
        )
        if assignment is not None:
            remove_contestant_computer_assignment(
                assignment=assignment,
                source="management_api",
                actor_identifier=request.management_user.get_username(),
            )
        return _no_content_response()

    _require_permissions(
        request.management_user,
        "ctrl.add_contestantcomputerassignment",
    )
    payload = _strict_json_object(
        request,
        fields={"computer_machine_id"},
        required={"computer_machine_id"},
    )
    computer_machine_id = _identifier(
        payload["computer_machine_id"],
        "computer_machine_id",
        max_length=40,
    )
    contestant = Student.objects.filter(userid=userid).first()
    if contestant is None:
        raise ManagementAPIError(
            400,
            "unknown_reference",
            field="userid",
        )
    computer = Computer.objects.filter(
        machine_id=computer_machine_id
    ).first()
    if computer is None:
        raise ManagementAPIError(
            400,
            "unknown_reference",
            field="computer_machine_id",
        )
    try:
        assignment, created = assign_contestant_to_computer(
            contestant=contestant,
            computer=computer,
            source="management_api",
            actor_identifier=request.management_user.get_username(),
        )
    except AssignmentConflict as error:
        raise ManagementAPIError(
            409,
            "assignment_conflict",
            detail=str(error),
        ) from error
    return _json_response(
        _assignment_json(assignment),
        status=201 if created else 200,
    )


@management_endpoint
def admin_account(request, username):
    """Read or atomically upsert a Control administrative login account."""

    _require_method(request, "GET", "PUT")
    username = _identifier(username, "username", max_length=150)
    User = get_user_model()

    if request.method == "GET":
        _require_permissions(request.management_user, "auth.view_user")
        user = User.objects.filter(username=username).first()
        if user is None:
            raise ManagementAPIError(404, "not_found")
        return _json_response(_admin_json(user))

    payload = _strict_json_object(
        request,
        fields={
            "first_name",
            "last_name",
            "email",
            "password",
            "enabled",
            "is_superuser",
        },
        required={
            "first_name",
            "last_name",
            "email",
            "enabled",
            "is_superuser",
        },
    )
    first_name = _string(
        payload, "first_name", allow_empty=True, max_length=150
    )
    last_name = _string(payload, "last_name", allow_empty=True, max_length=150)
    email = _string(payload, "email", allow_empty=True, max_length=254)
    enabled = _boolean(payload, "enabled")
    is_superuser = _boolean(payload, "is_superuser")
    password = None
    if "password" in payload:
        password = _string(
            payload,
            "password",
            max_length=4096,
            strip=False,
        )

    with transaction.atomic():
        user = User.objects.select_for_update().filter(username=username).first()
        created = user is None
        _require_permissions(
            request.management_user,
            "auth.add_user" if created else "auth.change_user",
        )

        # Django's model permissions intentionally do not imply the ability to
        # grant or modify superuser status.
        if is_superuser and not request.management_user.is_superuser:
            raise ManagementAPIError(403, "forbidden")
        if (
            user is not None
            and user.is_superuser
            and not request.management_user.is_superuser
        ):
            raise ManagementAPIError(403, "forbidden")
        if created and password is None:
            raise ManagementAPIError(400, "missing_fields", detail=["password"])
        if user is not None and user.pk == request.management_user.pk:
            if not enabled:
                raise ManagementAPIError(409, "cannot_disable_self")
            if user.is_superuser and not is_superuser:
                raise ManagementAPIError(409, "cannot_demote_self")

        if user is None:
            user = User(username=username)
        user.first_name = first_name
        user.last_name = last_name
        user.email = email
        user.is_active = enabled
        # Administrative logins must be staff to pass Django AdminSite's
        # has_permission check. Superuser status supplies every Control and
        # auth permission needed by both the custom GUI and Django admin.
        user.is_staff = True
        user.is_superuser = is_superuser
        if password is not None and not user.check_password(password):
            validate_password(password, user=user)
            user.set_password(password)
        user.full_clean()
        user.save()

    return _json_response(
        _admin_json(user),
        status=201 if created else 200,
        headers={"Location": request.path} if created else None,
    )

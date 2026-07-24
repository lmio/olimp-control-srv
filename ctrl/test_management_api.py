import base64
import json
import os
import uuid
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models import QuerySet
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .management.commands.bootstrap_dev_admin import USERNAME as DEV_ADMIN_USERNAME
from .assignments import assign_contestant_to_computer
from .models import (
    Computer,
    ContestantComputerAssignment,
    ContestantComputerAssignmentEvent,
    Location,
    Student,
)


ADMIN_PASSWORD = "Management-API-Test-Pass-948!"


def basic_header(username, password):
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {credentials}"


class ManagementAPITestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = get_user_model().objects.create_superuser(
            username="management-admin",
            password=ADMIN_PASSWORD,
            email="management@example.test",
        )
        self.authorization = basic_header(self.admin.username, ADMIN_PASSWORD)

    def request(self, method, name, *, args=(), payload=None, raw=None, content_type=None):
        path = reverse(name, args=args)
        body = raw
        if body is None and payload is not None:
            body = json.dumps(payload, separators=(",", ":"))
        return self.client.generic(
            method,
            path,
            data=body or "",
            content_type=content_type or "application/json",
            HTTP_AUTHORIZATION=self.authorization,
        )

    def put(self, name, args, payload):
        return self.request("PUT", name, args=args, payload=payload)

    def delete(self, name, args):
        return self.request("DELETE", name, args=args)


class ManagementAuthenticationTests(ManagementAPITestCase):
    def setUp(self):
        super().setUp()
        self.path = reverse("ctrl.api.management.state")

    def test_missing_malformed_bad_and_inactive_credentials_are_challenged(self):
        inactive = get_user_model().objects.create_superuser(
            username="inactive-management",
            password=ADMIN_PASSWORD,
            email="inactive@example.test",
        )
        inactive.is_active = False
        inactive.save(update_fields=["is_active"])
        headers = [
            None,
            "Bearer token",
            "Basic not-base64!",
            basic_header(self.admin.username, "wrong-password"),
            basic_header(inactive.username, ADMIN_PASSWORD),
        ]
        for header in headers:
            kwargs = {"HTTP_AUTHORIZATION": header} if header is not None else {}
            response = self.client.get(self.path, **kwargs)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json(), {"error": "authentication_required"})
            self.assertTrue(response.headers["WWW-Authenticate"].startswith("Basic "))
            self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_active_nonstaff_and_staff_without_permissions_are_forbidden(self):
        normal = get_user_model().objects.create_user(
            "normal-management", password=ADMIN_PASSWORD
        )
        staff = get_user_model().objects.create_user(
            "permissionless-management", password=ADMIN_PASSWORD, is_staff=True
        )
        for user in (normal, staff):
            response = self.client.get(
                self.path,
                HTTP_AUTHORIZATION=basic_header(user.username, ADMIN_PASSWORD),
            )
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json(), {"error": "forbidden"})

    def test_staff_with_all_view_permissions_can_read_state(self):
        staff = get_user_model().objects.create_user(
            "reader-management", password=ADMIN_PASSWORD, is_staff=True
        )
        staff.user_permissions.set(
            Permission.objects.filter(
                content_type__app_label="ctrl",
                codename__in={
                    "view_location",
                    "view_computer",
                    "view_student",
                    "view_contestantcomputerassignment",
                },
            )
        )
        response = self.client.get(
            self.path,
            HTTP_AUTHORIZATION=basic_header(staff.username, ADMIN_PASSWORD),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "classes": [],
                "computers": [],
                "students": [],
                "assignments": [],
            },
        )

    def test_basic_password_may_contain_colons(self):
        password = "Strong:Management:Password-948!"
        self.admin.set_password(password)
        self.admin.save(update_fields=["password"])
        response = self.client.get(
            self.path,
            HTTP_AUTHORIZATION=basic_header(self.admin.username, password),
        )
        self.assertEqual(response.status_code, 200)


class ManagementStrictJSONTests(ManagementAPITestCase):
    def setUp(self):
        super().setUp()
        self.class_id = str(uuid.uuid4())
        self.name = "ctrl.api.management.class"

    def test_wrong_method_is_json_405_with_allow_header(self):
        response = self.request("GET", self.name, args=[self.class_id])
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.json(), {"error": "method_not_allowed"})
        self.assertEqual(response.headers["Allow"], "PUT, DELETE")

    def test_non_json_content_type_is_rejected(self):
        response = self.request(
            "PUT",
            self.name,
            args=[self.class_id],
            raw='{"name":"A","sequence_num":1}',
            content_type="text/plain",
        )
        self.assertEqual(response.status_code, 415)
        self.assertEqual(response.json(), {"error": "unsupported_media_type"})

    def test_malformed_non_object_duplicate_and_non_finite_json_are_rejected(self):
        bodies = [
            "{not-json",
            "[]",
            '{"name":"A","name":"B","sequence_num":1}',
            '{"name":"A","sequence_num":NaN}',
        ]
        for body in bodies:
            response = self.request(
                "PUT", self.name, args=[self.class_id], raw=body
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"], "invalid_json")

    def test_unknown_missing_and_wrong_typed_fields_are_rejected(self):
        cases = [
            ({"name": "A", "sequence_num": 1, "extra": True}, "unknown_fields"),
            ({"name": "A"}, "missing_fields"),
            ({"name": "A", "sequence_num": True}, "invalid_field"),
            ({"name": "", "sequence_num": 1}, "invalid_field"),
        ]
        for payload, code in cases:
            response = self.put(self.name, [self.class_id], payload)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"], code)
        self.assertFalse(Location.objects.exists())

    def test_invalid_resource_uuid_is_json_error(self):
        response = self.put(
            self.name,
            ["not-a-uuid"],
            {"name": "A", "sequence_num": 1},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["field"], "id")


class ManagementResourceTests(ManagementAPITestCase):
    def test_class_put_is_idempotent_upsert(self):
        class_id = str(uuid.uuid4())
        path_name = "ctrl.api.management.class"
        payload = {"name": "Class A", "sequence_num": 2}

        created = self.put(path_name, [class_id], payload)
        repeated = self.put(path_name, [class_id], payload)
        updated = self.put(
            path_name,
            [class_id],
            {"name": "Renamed", "sequence_num": 7},
        )

        self.assertEqual(created.status_code, 201)
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(Location.objects.count(), 1)
        location = Location.objects.get()
        self.assertEqual(str(location.public_id), class_id)
        self.assertEqual(location.name, "Renamed")
        self.assertEqual(location.sequence_num, 7)
        self.assertEqual(updated.json()["id"], class_id)

    def test_upsert_requires_add_for_create_and_change_for_update(self):
        staff = get_user_model().objects.create_user(
            "limited-writer", password=ADMIN_PASSWORD, is_staff=True
        )
        add_permission = Permission.objects.get(
            content_type__app_label="ctrl", codename="add_location"
        )
        change_permission = Permission.objects.get(
            content_type__app_label="ctrl", codename="change_location"
        )
        staff.user_permissions.add(add_permission)
        authorization = basic_header(staff.username, ADMIN_PASSWORD)
        class_id = str(uuid.uuid4())
        path = reverse("ctrl.api.management.class", args=[class_id])
        body = json.dumps({"name": "A", "sequence_num": 1})

        created = self.client.put(
            path,
            body,
            content_type="application/json",
            HTTP_AUTHORIZATION=authorization,
        )
        denied_update = self.client.put(
            path,
            body,
            content_type="application/json",
            HTTP_AUTHORIZATION=authorization,
        )
        staff.user_permissions.remove(add_permission)
        staff.user_permissions.add(change_permission)
        allowed_update = self.client.put(
            path,
            json.dumps({"name": "B", "sequence_num": 2}),
            content_type="application/json",
            HTTP_AUTHORIZATION=authorization,
        )

        self.assertEqual(created.status_code, 201)
        self.assertEqual(denied_update.status_code, 403)
        self.assertEqual(allowed_update.status_code, 200)

    def test_class_put_cannot_shrink_grid_past_placed_computers(self):
        location = Location.objects.create(name="Grid class", grid_cols=6)
        Computer.objects.create(
            machine_id="far-column",
            name="Far PC",
            location=location,
            grid_row=1,
            grid_col=6,
        )
        name = "ctrl.api.management.class"

        rejected = self.put(
            name,
            [str(location.public_id)],
            {"name": location.name, "sequence_num": 0, "grid_cols": 3},
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.json()["error"], "validation_error")
        location.refresh_from_db()
        self.assertEqual(location.grid_cols, 6)

        accepted = self.put(
            name,
            [str(location.public_id)],
            {"name": location.name, "sequence_num": 0, "grid_cols": 8},
        )
        self.assertEqual(accepted.status_code, 200)
        location.refresh_from_db()
        self.assertEqual(location.grid_cols, 8)

    def test_delete_requires_delete_permission_and_is_idempotent(self):
        location = Location.objects.create(name="Delete me")
        staff = get_user_model().objects.create_user(
            "limited-deleter", password=ADMIN_PASSWORD, is_staff=True
        )
        authorization = basic_header(staff.username, ADMIN_PASSWORD)
        path = reverse("ctrl.api.management.class", args=[location.public_id])

        denied = self.client.delete(path, HTTP_AUTHORIZATION=authorization)
        self.assertEqual(denied.status_code, 403)
        self.assertTrue(Location.objects.filter(pk=location.pk).exists())

        staff.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="ctrl", codename="delete_location"
            )
        )
        still_denied = self.client.delete(
            path,
            HTTP_AUTHORIZATION=authorization,
        )
        self.assertEqual(still_denied.status_code, 403)
        staff.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="ctrl", codename="change_computer"
            )
        )
        deleted = self.client.delete(path, HTTP_AUTHORIZATION=authorization)
        repeated = self.client.delete(path, HTTP_AUTHORIZATION=authorization)
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(deleted.content, b"")
        self.assertEqual(deleted.headers["Cache-Control"], "no-store")
        self.assertEqual(repeated.status_code, 204)
        self.assertFalse(Location.objects.filter(pk=location.pk).exists())

    def test_delete_supports_students_computers_and_classes(self):
        location = Location.objects.create(name="Class")
        computer = Computer.objects.create(
            machine_id="delete-pc", name="PC", location=location
        )
        Student.objects.create(userid="delete-student")

        resources = [
            ("ctrl.api.management.student", "delete-student"),
            ("ctrl.api.management.computer", "delete-pc"),
            ("ctrl.api.management.class", str(location.public_id)),
        ]
        for name, identifier in resources:
            first = self.delete(name, [identifier])
            second = self.delete(name, [identifier])
            self.assertEqual(first.status_code, 204)
            self.assertEqual(second.status_code, 204)

        self.assertFalse(Student.objects.filter(userid="delete-student").exists())
        self.assertFalse(Computer.objects.filter(pk=computer.pk).exists())
        self.assertFalse(Location.objects.filter(pk=location.pk).exists())

    def test_delete_locks_only_the_student_or_computer_row(self):
        select_for_update = QuerySet.select_for_update
        lock_calls = []

        def capture_lock_scope(queryset, *args, **kwargs):
            lock_calls.append((queryset.model, kwargs.get("of")))
            return select_for_update(queryset, *args, **kwargs)

        with patch.object(
            QuerySet,
            "select_for_update",
            new=capture_lock_scope,
        ):
            missing_student = self.delete(
                "ctrl.api.management.student",
                ["missing-student"],
            )
            missing_computer = self.delete(
                "ctrl.api.management.computer",
                ["missing-computer"],
            )

        self.assertEqual(missing_student.status_code, 204)
        self.assertEqual(missing_computer.status_code, 204)
        self.assertIn((Student, ("self",)), lock_calls)
        self.assertIn((Computer, ("self",)), lock_calls)

    def test_class_delete_uses_existing_safe_unassignment_semantics(self):
        location = Location.objects.create(name="Class")
        computer = Computer.objects.create(
            machine_id="retained-pc", name="PC", location=location
        )
        response = self.delete(
            "ctrl.api.management.class", [str(location.public_id)]
        )

        self.assertEqual(response.status_code, 204)
        computer.refresh_from_db()
        self.assertIsNone(computer.location_id)

    def test_computer_put_assigns_and_unassigns_class_and_clears_grid(self):
        first = Location.objects.create(name="First")
        second = Location.objects.create(name="Second")
        name = "ctrl.api.management.computer"
        create_payload = {
            "name": "PC 1",
            "class_id": str(first.public_id),
            "sequence_num": 1,
        }

        created = self.put(name, ["machine-1"], create_payload)
        repeated = self.put(name, ["machine-1"], create_payload)
        computer = Computer.objects.get(machine_id="machine-1")
        computer.grid_row = 3
        computer.grid_col = 4
        computer.save(update_fields=["grid_row", "grid_col"])
        reassigned = self.put(
            name,
            ["machine-1"],
            {
                "name": "Renamed PC",
                "class_id": str(second.public_id),
                "sequence_num": 9,
            },
        )
        unassigned = self.put(
            name,
            ["machine-1"],
            {"name": "Renamed PC", "class_id": None, "sequence_num": 9},
        )

        self.assertEqual(created.status_code, 201)
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(reassigned.status_code, 200)
        self.assertEqual(unassigned.status_code, 200)
        self.assertEqual(Computer.objects.count(), 1)
        computer.refresh_from_db()
        self.assertIsNone(computer.location)
        self.assertIsNone(computer.grid_row)
        self.assertIsNone(computer.grid_col)
        self.assertIsNone(unassigned.json()["class_id"])

    def test_computer_unknown_class_rolls_back(self):
        response = self.put(
            "ctrl.api.management.computer",
            ["machine-1"],
            {
                "name": "PC 1",
                "class_id": str(uuid.uuid4()),
                "sequence_num": 1,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "unknown_reference")
        self.assertFalse(Computer.objects.exists())

    def test_computer_put_rejects_embedded_contestant_mapping(self):
        base = {"name": "PC 1", "class_id": None, "sequence_num": 1}

        response = self.put(
            "ctrl.api.management.computer",
            ["machine-1"],
            {**base, "student_userid": "student"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "unknown_fields")
        self.assertFalse(Computer.objects.exists())

    def test_student_put_is_identifier_only_idempotent_upsert(self):
        name = "ctrl.api.management.student"

        created = self.put(name, ["student"], {})
        repeated = self.put(name, ["student"], {})

        self.assertEqual(created.status_code, 201)
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(Student.objects.count(), 1)
        self.assertEqual(
            {
                key: value
                for key, value in created.json().items()
                if key != "id"
            },
            {
                "userid": "student",
                "computer_id": None,
                "class_id": None,
                "anomalies": ["no_computers"],
            },
        )
        self.assertEqual(repeated.json(), created.json())

    def test_student_and_mapping_use_separate_permissions(self):
        computer = Computer.objects.create(machine_id="permission-pc", name="PC")
        limited = get_user_model().objects.create_user(
            "student-writer",
            password=ADMIN_PASSWORD,
            is_staff=True,
        )
        limited.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="ctrl",
                codename="add_student",
            )
        )
        authorization = basic_header(limited.username, ADMIN_PASSWORD)
        student_path = reverse(
            "ctrl.api.management.student",
            args=["student"],
        )
        assignment_path = reverse(
            "ctrl.api.management.assignment",
            args=["student"],
        )

        created = self.client.put(
            student_path,
            "{}",
            content_type="application/json",
            HTTP_AUTHORIZATION=authorization,
        )
        denied = self.client.put(
            assignment_path,
            json.dumps({"computer_machine_id": computer.machine_id}),
            content_type="application/json",
            HTTP_AUTHORIZATION=authorization,
        )

        self.assertEqual(created.status_code, 201)
        self.assertEqual(denied.status_code, 403)
        self.assertFalse(ContestantComputerAssignment.objects.exists())

        limited.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="ctrl",
                codename="add_contestantcomputerassignment",
            )
        )
        allowed = self.client.put(
            assignment_path,
            json.dumps({"computer_machine_id": computer.machine_id}),
            content_type="application/json",
            HTTP_AUTHORIZATION=authorization,
        )
        self.assertEqual(allowed.status_code, 201)
        self.assertEqual(
            ContestantComputerAssignment.objects.get().contestant.userid,
            "student",
        )

    def test_student_rejects_legacy_detail_and_assignment_fields(self):
        name = "ctrl.api.management.student"
        for payload in (
            {"name": "Student"},
            {"surname": "Contestant"},
            {"computer_ids": ["pc-a"]},
            {"class_ids": [str(uuid.uuid4())]},
        ):
            response = self.put(name, ["student"], payload)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"], "unknown_fields")
        self.assertFalse(Student.objects.exists())

    def test_student_identifier_accepts_independent_userids(self):
        for userid in ("student.name", "mokinys-ą", "student@example.test"):
            accepted = self.put(
                "ctrl.api.management.student", [userid], {}
            )
            self.assertEqual(accepted.status_code, 201)
        self.assertTrue(
            Student.objects.filter(userid="student@example.test").exists()
        )

    def test_mapping_put_is_idempotent_and_records_one_assignment_event(self):
        student = Student.objects.create(userid="student")
        computer = Computer.objects.create(machine_id="pc-a", name="PC A")
        name = "ctrl.api.management.assignment"
        payload = {"computer_machine_id": computer.machine_id}

        created = self.put(name, [student.userid], payload)
        repeated = self.put(name, [student.userid], payload)

        self.assertEqual(created.status_code, 201)
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(created.json()["contestant_identifier"], student.userid)
        self.assertEqual(created.json()["computer_machine_id"], computer.machine_id)
        self.assertEqual(created.json()["source"], "management_api")
        self.assertEqual(repeated.json(), created.json())
        self.assertEqual(ContestantComputerAssignment.objects.count(), 1)
        event = ContestantComputerAssignmentEvent.objects.get()
        self.assertEqual(
            event.action,
            ContestantComputerAssignmentEvent.Action.ASSIGNED,
        )
        self.assertEqual(event.source, "management_api")
        self.assertEqual(event.actor_identifier, self.admin.username)

    def test_mapping_conflicts_require_explicit_removal_before_reassignment(self):
        student = Student.objects.create(userid="student")
        other_student = Student.objects.create(userid="other-student")
        first = Computer.objects.create(machine_id="pc-a", name="PC A")
        second = Computer.objects.create(machine_id="pc-b", name="PC B")
        name = "ctrl.api.management.assignment"

        created = self.put(
            name,
            [student.userid],
            {"computer_machine_id": first.machine_id},
        )
        contestant_conflict = self.put(
            name,
            [student.userid],
            {"computer_machine_id": second.machine_id},
        )
        computer_conflict = self.put(
            name,
            [other_student.userid],
            {"computer_machine_id": first.machine_id},
        )

        self.assertEqual(created.status_code, 201)
        for response in (contestant_conflict, computer_conflict):
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["error"], "assignment_conflict")
            self.assertIn("Remove that mapping before", response.json()["detail"])
        assignment = ContestantComputerAssignment.objects.get()
        self.assertEqual(assignment.contestant, student)
        self.assertEqual(assignment.computer, first)

        removed = self.delete(name, [student.userid])
        repeated_removal = self.delete(name, [student.userid])
        reassigned = self.put(
            name,
            [student.userid],
            {"computer_machine_id": second.machine_id},
        )

        self.assertEqual(removed.status_code, 204)
        self.assertEqual(repeated_removal.status_code, 204)
        self.assertEqual(reassigned.status_code, 201)
        assignment = ContestantComputerAssignment.objects.get()
        self.assertEqual(assignment.contestant, student)
        self.assertEqual(assignment.computer, second)
        events = list(
            ContestantComputerAssignmentEvent.objects.values_list(
                "action",
                "computer_identifier",
                "source",
                "actor_identifier",
            )
        )
        self.assertEqual(
            events,
            [
                ("assigned", "pc-a", "management_api", self.admin.username),
                ("removed", "pc-a", "management_api", self.admin.username),
                ("assigned", "pc-b", "management_api", self.admin.username),
            ],
        )

    def test_mapping_rejects_unknown_references_and_invalid_payload(self):
        student = Student.objects.create(userid="student")
        Computer.objects.create(machine_id="pc-a", name="PC A")
        name = "ctrl.api.management.assignment"
        cases = [
            (
                "missing-student",
                {"computer_machine_id": "pc-a"},
                "userid",
            ),
            (
                student.userid,
                {"computer_machine_id": "missing-computer"},
                "computer_machine_id",
            ),
        ]
        for userid, payload, field in cases:
            response = self.put(name, [userid], payload)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"], "unknown_reference")
            self.assertEqual(response.json()["field"], field)

        for payload in ({}, {"computer_machine_id": "pc-a", "extra": True}):
            response = self.put(name, [student.userid], payload)
            self.assertEqual(response.status_code, 400)
        self.assertFalse(ContestantComputerAssignment.objects.exists())

    def test_mapped_contestant_and_computer_must_be_unmapped_before_delete(self):
        student = Student.objects.create(userid="student")
        computer = Computer.objects.create(machine_id="pc-a", name="PC A")
        assign_contestant_to_computer(
            contestant=student,
            computer=computer,
            source="test",
        )

        student_delete = self.delete(
            "ctrl.api.management.student",
            [student.userid],
        )
        computer_delete = self.delete(
            "ctrl.api.management.computer",
            [computer.machine_id],
        )

        for response in (student_delete, computer_delete):
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["error"], "assignment_exists")
            self.assertIn("Remove that mapping first", response.json()["detail"])
        self.assertTrue(Student.objects.filter(pk=student.pk).exists())
        self.assertTrue(Computer.objects.filter(pk=computer.pk).exists())

    def test_state_is_deterministic_derived_and_secret_free(self):
        later = Location.objects.create(name="Later", sequence_num=2)
        earlier = Location.objects.create(name="Earlier", sequence_num=1)
        later_pc = Computer.objects.create(
            machine_id="pc-z", name="Later PC", location=later, sequence_num=2
        )
        earlier_pc = Computer.objects.create(
            machine_id="pc-a", name="Earlier PC", location=earlier, sequence_num=1
        )
        student = Student.objects.create(userid="student")
        assignment, _ = assign_contestant_to_computer(
            contestant=student,
            computer=earlier_pc,
            source="test",
        )

        response = self.client.get(
            reverse("ctrl.api.management.state"),
            HTTP_AUTHORIZATION=self.authorization,
        )

        self.assertEqual(response.status_code, 200)
        state = response.json()
        self.assertEqual(
            [item["id"] for item in state["classes"]],
            [str(earlier.public_id), str(later.public_id)],
        )
        self.assertEqual(
            [item["machine_id"] for item in state["computers"]],
            ["pc-a", "pc-z"],
        )
        self.assertEqual(state["students"][0]["computer_id"], "pc-a")
        self.assertEqual(
            state["students"][0]["class_id"],
            str(earlier.public_id),
        )
        self.assertEqual(state["students"][0]["anomalies"], [])
        self.assertEqual(state["students"][0]["userid"], "student")
        self.assertEqual(state["computers"][0]["student_userid"], "student")
        self.assertIsNone(state["computers"][1]["student_userid"])
        self.assertEqual(
            state["assignments"],
            [
                {
                    "contestant_identifier": "student",
                    "computer_machine_id": "pc-a",
                    "assigned_at": assignment.assigned_at.isoformat(),
                    "source": "test",
                }
            ],
        )
        serialized = response.content.decode()
        self.assertNotIn("password", serialized.lower())


class ManagementAdminAccountTests(ManagementAPITestCase):
    name = "ctrl.api.management.admin"

    def payload(self, **overrides):
        payload = {
            "first_name": "Control",
            "last_name": "Administrator",
            "email": "control-admin@example.test",
            "enabled": True,
            "is_superuser": True,
        }
        payload.update(overrides)
        return payload

    def test_put_get_and_update_create_a_real_secret_free_admin_login(self):
        password = "Control-Administrator-Pass-948!"
        created = self.put(
            self.name,
            ["control-admin"],
            self.payload(password=password),
        )
        user = get_user_model().objects.get(username="control-admin")
        original_password_hash = user.password
        repeated_create = self.put(
            self.name,
            ["control-admin"],
            self.payload(password=password),
        )
        user.refresh_from_db()

        self.assertEqual(created.status_code, 201)
        self.assertEqual(
            created.headers["Location"],
            reverse(self.name, args=["control-admin"]),
        )
        self.assertEqual(repeated_create.status_code, 200)
        self.assertEqual(user.password, original_password_hash)
        self.assertEqual(
            created.json(),
            {
                "username": "control-admin",
                "first_name": "Control",
                "last_name": "Administrator",
                "email": "control-admin@example.test",
                "enabled": True,
                "is_staff": True,
                "is_superuser": True,
            },
        )
        self.assertTrue(user.check_password(password))
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.has_perms(
            (
                "ctrl.add_location",
                "ctrl.change_student",
                "ctrl.delete_computer",
                "auth.change_user",
            )
        ))

        self.assertTrue(
            self.client.login(username="control-admin", password=password)
        )
        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 200)
        self.assertEqual(
            self.client.get(reverse("ctrl.location_list")).status_code,
            200,
        )
        self.client.logout()

        fetched = self.request("GET", self.name, args=["control-admin"])
        update_payload = self.payload(
            first_name="Renamed",
            last_name="Admin",
            email="renamed@example.test",
        )
        updated = self.put(self.name, ["control-admin"], update_payload)
        repeated = self.put(self.name, ["control-admin"], update_payload)

        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json(), created.json())
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(get_user_model().objects.filter(
            username="control-admin"
        ).count(), 1)
        user.refresh_from_db()
        self.assertEqual(user.password, original_password_hash)
        self.assertEqual(user.first_name, "Renamed")
        self.assertEqual(user.last_name, "Admin")
        self.assertEqual(user.email, "renamed@example.test")

        for response in (created, repeated_create, fetched, updated, repeated):
            serialized = response.content.decode()
            self.assertNotIn("password", serialized.lower())
            self.assertNotIn(password, serialized)
            self.assertNotIn(original_password_hash, serialized)
            self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_password_is_required_on_create_optional_on_update_and_validated(self):
        missing = self.put(self.name, ["missing-password"], self.payload())
        weak = self.put(
            self.name,
            ["weak-password"],
            self.payload(password="password"),
        )

        self.assertEqual(missing.status_code, 400)
        self.assertEqual(
            missing.json(),
            {"error": "missing_fields", "detail": ["password"]},
        )
        self.assertEqual(weak.status_code, 400)
        self.assertEqual(weak.json()["error"], "validation_error")
        self.assertFalse(
            get_user_model().objects.filter(
                username__in=["missing-password", "weak-password"]
            ).exists()
        )

        old_password = "Old-Control-Admin-Pass-948!"
        new_password = "New-Control-Admin-Pass-731!"
        self.assertEqual(
            self.put(
                self.name,
                ["password-update"],
                self.payload(password=old_password),
            ).status_code,
            201,
        )
        updated = self.put(
            self.name,
            ["password-update"],
            self.payload(password=new_password),
        )
        user = get_user_model().objects.get(username="password-update")
        self.assertEqual(updated.status_code, 200)
        self.assertFalse(user.check_password(old_password))
        self.assertTrue(user.check_password(new_password))

    def test_contract_is_strict_and_get_not_found_is_json(self):
        path = reverse(self.name, args=["strict-admin"])
        wrong_method = self.client.post(
            path,
            HTTP_AUTHORIZATION=self.authorization,
        )
        missing = self.client.get(path, HTTP_AUTHORIZATION=self.authorization)
        unknown = self.put(
            self.name,
            ["strict-admin"],
            self.payload(
                password="Strict-Control-Admin-Pass-948!",
                unexpected=True,
            ),
        )
        wrong_type = self.put(
            self.name,
            ["strict-admin"],
            self.payload(
                password="Strict-Control-Admin-Pass-948!",
                enabled="yes",
            ),
        )
        invalid_email = self.put(
            self.name,
            ["strict-admin"],
            self.payload(
                password="Strict-Control-Admin-Pass-948!",
                email="not-an-email",
            ),
        )

        self.assertEqual(wrong_method.status_code, 405)
        self.assertEqual(wrong_method.json(), {"error": "method_not_allowed"})
        self.assertEqual(wrong_method.headers["Allow"], "GET, PUT")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json(), {"error": "not_found"})
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(unknown.json()["error"], "unknown_fields")
        self.assertEqual(wrong_type.status_code, 400)
        self.assertEqual(wrong_type.json(), {
            "error": "invalid_field",
            "field": "enabled",
        })
        self.assertEqual(invalid_email.status_code, 400)
        self.assertEqual(invalid_email.json()["error"], "validation_error")
        self.assertFalse(
            get_user_model().objects.filter(username="strict-admin").exists()
        )

    def test_django_permissions_are_enforced_without_privilege_escalation(self):
        User = get_user_model()
        limited = User.objects.create_user(
            "limited-admin-manager",
            password=ADMIN_PASSWORD,
            is_staff=True,
        )
        authorization = basic_header(limited.username, ADMIN_PASSWORD)
        path = reverse(self.name, args=["managed-admin"])
        password = "Managed-Control-Admin-Pass-948!"
        body = json.dumps(self.payload(password=password))

        denied = self.client.put(
            path,
            body,
            content_type="application/json",
            HTTP_AUTHORIZATION=authorization,
        )
        self.assertEqual(denied.status_code, 403)
        self.assertFalse(User.objects.filter(username="managed-admin").exists())

        limited.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="auth",
                codename="add_user",
            )
        )
        escalation = self.client.put(
            path,
            body,
            content_type="application/json",
            HTTP_AUTHORIZATION=authorization,
        )
        self.assertEqual(escalation.status_code, 403)
        self.assertFalse(User.objects.filter(username="managed-admin").exists())

        limited_create = self.client.put(
            path,
            json.dumps(self.payload(password=password, is_superuser=False)),
            content_type="application/json",
            HTTP_AUTHORIZATION=authorization,
        )
        self.assertEqual(limited_create.status_code, 201)
        managed = User.objects.get(username="managed-admin")
        self.assertTrue(managed.is_staff)
        self.assertFalse(managed.is_superuser)

        denied_get = self.client.get(
            path,
            HTTP_AUTHORIZATION=authorization,
        )
        self.assertEqual(denied_get.status_code, 403)
        limited.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="auth",
                codename="view_user",
            ),
            Permission.objects.get(
                content_type__app_label="auth",
                codename="change_user",
            ),
        )
        allowed_get = self.client.get(
            path,
            HTTP_AUTHORIZATION=authorization,
        )
        allowed_update = self.client.put(
            path,
            json.dumps(self.payload(is_superuser=False, email="new@example.test")),
            content_type="application/json",
            HTTP_AUTHORIZATION=authorization,
        )
        self.assertEqual(allowed_get.status_code, 200)
        self.assertEqual(allowed_update.status_code, 200)

        superuser_path = reverse(self.name, args=[self.admin.username])
        protected_superuser = self.client.put(
            superuser_path,
            json.dumps(self.payload(is_superuser=False)),
            content_type="application/json",
            HTTP_AUTHORIZATION=authorization,
        )
        self.assertEqual(protected_superuser.status_code, 403)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_superuser)

    def test_authenticated_admin_cannot_disable_or_demote_itself(self):
        payload = {
            "first_name": self.admin.first_name,
            "last_name": self.admin.last_name,
            "email": self.admin.email,
            "enabled": True,
            "is_superuser": True,
        }
        disable = self.put(
            self.name,
            [self.admin.username],
            {**payload, "enabled": False},
        )
        demote = self.put(
            self.name,
            [self.admin.username],
            {**payload, "is_superuser": False},
        )

        self.assertEqual(disable.status_code, 409)
        self.assertEqual(disable.json(), {"error": "cannot_disable_self"})
        self.assertEqual(demote.status_code, 409)
        self.assertEqual(demote.json(), {"error": "cannot_demote_self"})
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)
        self.assertTrue(self.admin.is_staff)
        self.assertTrue(self.admin.is_superuser)


class BootstrapDevAdminCommandTests(TestCase):
    def call(self, environment):
        output = StringIO()
        with patch.dict(os.environ, environment, clear=False):
            call_command("bootstrap_dev_admin", stdout=output)
        return output.getvalue()

    def test_command_requires_explicit_gate_and_password(self):
        with self.assertRaisesRegex(CommandError, "CTRL_BOOTSTRAP_DEV_ADMIN=1"):
            self.call(
                {
                    "CTRL_BOOTSTRAP_DEV_ADMIN": "",
                    "CTRL_DEV_ADMIN_PASSWORD": "",
                }
            )
        with self.assertRaisesRegex(CommandError, "CTRL_DEV_ADMIN_PASSWORD"):
            self.call(
                {
                    "CTRL_BOOTSTRAP_DEV_ADMIN": "1",
                    "CTRL_DEV_ADMIN_PASSWORD": "",
                }
            )
        self.assertFalse(get_user_model().objects.exists())

    @override_settings(CTRL_PRODUCTION=True)
    def test_command_refuses_production_even_when_enabled(self):
        with self.assertRaisesRegex(CommandError, "in production"):
            self.call(
                {
                    "CTRL_BOOTSTRAP_DEV_ADMIN": "1",
                    "CTRL_DEV_ADMIN_PASSWORD": "Strong-Dev-Admin-Pass-948!",
                }
            )
        self.assertFalse(get_user_model().objects.exists())

    def test_command_only_bootstraps_idempotent_control_admin(self):
        environment = {
            "CTRL_BOOTSTRAP_DEV_ADMIN": "1",
            "CTRL_DEV_ADMIN_PASSWORD": "Strong-Dev-Admin-Pass-948!",
        }
        first_output = self.call(environment)
        second_output = self.call(environment)

        self.assertEqual(get_user_model().objects.count(), 1)
        self.assertEqual(Location.objects.count(), 0)
        self.assertEqual(Computer.objects.count(), 0)
        self.assertEqual(Student.objects.count(), 0)
        user = get_user_model().objects.get(username=DEV_ADMIN_USERNAME)
        self.assertTrue(user.is_active)
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.check_password(environment["CTRL_DEV_ADMIN_PASSWORD"]))
        self.assertIn("Bootstrapped local dev-admin", first_output)
        self.assertNotIn(environment["CTRL_DEV_ADMIN_PASSWORD"], first_output)
        self.assertNotIn(environment["CTRL_DEV_ADMIN_PASSWORD"], second_output)

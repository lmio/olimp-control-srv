import hashlib
import hmac
import json
import time

from django.contrib.auth import get_user_model
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from .assignments import (
    AssignmentConflict,
    assign_contestant_to_computer,
    remove_contestant_computer_assignment,
)
from .models import (
    Computer,
    ContestantComputerAssignment,
    ContestantComputerAssignmentEvent,
    Location,
    Student,
    UnknownComputer,
)
from ctrl_srv.settings import validate_service_keys


TEST_TOILET_KEY = "isolated-test-toilet-key"


class ModelTests(TestCase):
    def test_production_rejects_reused_machine_and_toilet_service_key(self):
        with self.assertRaisesRegex(ValueError, "must differ"):
            validate_service_keys(True, "shared", "shared")
        with self.assertRaisesRegex(ValueError, "at least 32"):
            validate_service_keys(True, "machine", "toilet")
        with self.assertRaisesRegex(ValueError, "sample or development"):
            validate_service_keys(
                True,
                "m" * 32,
                "a_different_very_secret_toilet_service_key",
            )
        validate_service_keys(True, "m" * 32, "t" * 32)

    def test_location_public_id_is_distinct_and_survives_rename(self):
        first = Location.objects.create(name="101")
        second = Location.objects.create(name="102")
        original_id = first.public_id

        first.name = "Renamed"
        first.save(update_fields=["name"])
        first.refresh_from_db()

        self.assertEqual(first.public_id, original_id)
        self.assertNotEqual(first.public_id, second.public_id)

    def test_machine_id_is_unique(self):
        Computer.objects.create(machine_id="same-mid", name="First")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Computer.objects.create(machine_id="same-mid", name="Second")

    def test_location_delete_nulls_computer_location(self):
        location = Location.objects.create(name="101")
        computer = Computer.objects.create(
            machine_id="mid-1",
            name="PC 1",
            location=location,
            grid_row=2,
            grid_col=3,
        )

        location.delete()
        computer.refresh_from_db()

        self.assertIsNone(computer.location_id)
        self.assertIsNone(computer.grid_row)
        self.assertIsNone(computer.grid_col)

    def test_direct_computer_location_change_clears_grid_position(self):
        old = Location.objects.create(name="Old")
        new = Location.objects.create(name="New")
        computer = Computer.objects.create(
            machine_id="direct-move",
            name="PC",
            location=old,
            grid_row=2,
            grid_col=3,
        )

        computer.location = new
        computer.save(update_fields=["location"])
        computer.refresh_from_db()

        self.assertEqual(computer.location, new)
        self.assertIsNone(computer.grid_row)
        self.assertIsNone(computer.grid_col)

    def test_physical_grid_constraints_reject_partial_and_duplicate_positions(self):
        location = Location.objects.create(name="101", grid_cols=4)
        Computer.objects.create(
            machine_id="grid-first",
            name="First",
            location=location,
            grid_row=1,
            grid_col=1,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            Computer.objects.create(
                machine_id="grid-duplicate",
                name="Duplicate",
                location=location,
                grid_row=1,
                grid_col=1,
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            Computer.objects.create(
                machine_id="grid-partial",
                name="Partial",
                location=location,
                grid_row=2,
                grid_col=None,
            )

    def test_computer_validation_rejects_position_outside_location_grid(self):
        location = Location.objects.create(name="Bounded grid", grid_cols=4)
        computer = Computer(
            machine_id="outside-grid",
            name="Outside",
            location=location,
            grid_row=1,
            grid_col=5,
        )

        with self.assertRaisesRegex(ValidationError, "outside the configured"):
            computer.full_clean()

    def test_student_anomaly_codes(self):
        location = Location.objects.create(name="101")
        computer = Computer.objects.create(machine_id="mid-1", name="PC 1")
        student = Student.objects.create(userid="student")

        self.assertEqual(student.assignment_anomalies, ["no_computers"])
        assignment, created = assign_contestant_to_computer(
            contestant=student,
            computer=computer,
            source="test",
        )
        self.assertTrue(created)
        student.refresh_from_db()
        self.assertEqual(student.assignment_anomalies, ["computer_without_class"])
        computer.location = location
        computer.save(update_fields=["location"])
        student.refresh_from_db()
        self.assertEqual(student.assignment_anomalies, [])

        repeated, created = assign_contestant_to_computer(
            contestant=student,
            computer=computer,
            source="test",
        )
        self.assertFalse(created)
        self.assertEqual(repeated.pk, assignment.pk)
        self.assertEqual(
            ContestantComputerAssignmentEvent.objects.filter(
                action="assigned"
            ).count(),
            1,
        )

    def test_assignment_is_strict_one_to_one_and_requires_explicit_removal(self):
        alice = Student.objects.create(userid="alice")
        bob = Student.objects.create(userid="bob")
        first = Computer.objects.create(machine_id="mid-1", name="PC 1")
        second = Computer.objects.create(machine_id="mid-2", name="PC 2")
        assignment, _ = assign_contestant_to_computer(
            contestant=alice,
            computer=first,
            source="test",
        )

        with self.assertRaisesRegex(AssignmentConflict, "Remove that mapping"):
            assign_contestant_to_computer(
                contestant=alice,
                computer=second,
                source="test",
            )
        with self.assertRaisesRegex(AssignmentConflict, "Remove that mapping"):
            assign_contestant_to_computer(
                contestant=bob,
                computer=first,
                source="test",
            )
        self.assertEqual(ContestantComputerAssignment.objects.count(), 1)

        remove_contestant_computer_assignment(
            assignment=assignment,
            source="test_remove",
        )
        replacement, created = assign_contestant_to_computer(
            contestant=alice,
            computer=second,
            source="test",
        )
        self.assertTrue(created)
        self.assertEqual(replacement.computer, second)
        self.assertEqual(
            list(
                ContestantComputerAssignmentEvent.objects.values_list(
                    "action", "computer_identifier"
                )
            ),
            [
                ("assigned", "mid-1"),
                ("removed", "mid-1"),
                ("assigned", "mid-2"),
            ],
        )


@override_settings(
    CTRL_TOILET_AUTH_KEY=TEST_TOILET_KEY,
    CTRL_TOILET_AUTH_MAX_SKEW_SECONDS=60,
)
class ToiletApiTestCase(TestCase):
    def setUp(self):
        self.client = Client()

    def _request_signature(self, timestamp, method, path, body, key=TEST_TOILET_KEY):
        canonical = (
            timestamp.encode()
            + b"\n"
            + method.upper().encode()
            + b"\n"
            + path.encode()
            + b"\n"
            + body
        )
        return hmac.new(key.encode(), canonical, hashlib.sha256).hexdigest()

    def signed_request(self, method, path, payload=None, timestamp=None, signature=None):
        timestamp = str(int(time.time()) if timestamp is None else timestamp)
        body = b""
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode()
        if signature is None:
            signature = self._request_signature(timestamp, method, path, body)
        response = self.client.generic(
            method,
            path,
            data=body,
            content_type="application/json",
            HTTP_X_LMIO_TOILET_TIMESTAMP=timestamp,
            HTTP_X_LMIO_TOILET_AUTH=signature,
        )
        return response, timestamp

    def assert_signed_response(self, response, timestamp):
        canonical = (
            timestamp.encode()
            + b"\n"
            + str(response.status_code).encode()
            + b"\n"
            + response.content
        )
        expected = hmac.new(
            TEST_TOILET_KEY.encode(), canonical, hashlib.sha256
        ).hexdigest()
        self.assertEqual(response.headers["X-LMIO-Toilet-Auth"], expected)


class ServiceAuthenticationTests(ToiletApiTestCase):
    def setUp(self):
        super().setUp()
        self.path = reverse("ctrl.api.toilet.classes")

    def test_valid_signature_and_signed_response(self):
        response, timestamp = self.signed_request("GET", self.path)
        self.assertEqual(response.status_code, 200)
        self.assert_signed_response(response, timestamp)

    def test_missing_signature_is_rejected_and_response_is_signed(self):
        timestamp = str(int(time.time()))
        response = self.client.get(
            self.path,
            HTTP_X_LMIO_TOILET_TIMESTAMP=timestamp,
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"error": "invalid_service_auth"})
        self.assert_signed_response(response, timestamp)

    def test_bad_signature_is_rejected(self):
        response, timestamp = self.signed_request(
            "GET", self.path, signature="0" * 64
        )
        self.assertEqual(response.status_code, 403)
        self.assert_signed_response(response, timestamp)

    def test_stale_signature_is_rejected(self):
        response, timestamp = self.signed_request(
            "GET", self.path, timestamp=int(time.time()) - 61
        )
        self.assertEqual(response.status_code, 403)
        self.assert_signed_response(response, timestamp)

    def test_method_error_is_signed(self):
        response, timestamp = self.signed_request("POST", self.path, payload={})
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.headers["Allow"], "GET")
        self.assert_signed_response(response, timestamp)

    def test_malformed_json_error_is_signed(self):
        path = reverse("ctrl.api.toilet.student_assignment")
        timestamp = str(int(time.time()))
        body = b"{not-json"
        signature = self._request_signature(timestamp, "POST", path, body)
        response = self.client.generic(
            "POST",
            path,
            data=body,
            content_type="application/json",
            HTTP_X_LMIO_TOILET_TIMESTAMP=timestamp,
            HTTP_X_LMIO_TOILET_AUTH=signature,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "invalid_request"})
        self.assert_signed_response(response, timestamp)


class ClassCatalogApiTests(ToiletApiTestCase):
    def test_catalog_is_deterministic_and_rename_keeps_id(self):
        later = Location.objects.create(name="B", sequence_num=2)
        earlier = Location.objects.create(name="Z", sequence_num=1)
        same_sequence = Location.objects.create(name="A", sequence_num=2)
        original_id = str(earlier.public_id)
        path = reverse("ctrl.api.toilet.classes")

        response, _ = self.signed_request("GET", path)
        self.assertEqual(
            [item["id"] for item in response.json()["classes"]],
            [str(earlier.public_id), str(same_sequence.public_id), str(later.public_id)],
        )

        earlier.name = "Renamed"
        earlier.save(update_fields=["name"])
        response, _ = self.signed_request("GET", path)
        renamed = response.json()["classes"][0]
        self.assertEqual(renamed["id"], original_id)
        self.assertEqual(renamed["name"], "Renamed")

        earlier.delete()
        response, _ = self.signed_request("GET", path)
        self.assertNotIn(original_id, [item["id"] for item in response.json()["classes"]])


class RosterAndLayoutApiTests(ToiletApiTestCase):
    def test_roster_and_layout_include_live_student_and_physical_positions(self):
        location = Location.objects.create(
            name="101",
            sequence_num=3,
            grid_cols=5,
        )
        student = Student.objects.create(userid="alice")
        first = Computer.objects.create(
            machine_id="mid-2",
            name="PC 2",
            location=location,
            sequence_num=2,
            grid_row=1,
            grid_col=2,
        )
        second = Computer.objects.create(
            machine_id="mid-1",
            name="PC 1",
            location=location,
            sequence_num=1,
            grid_row=1,
            grid_col=1,
        )
        assign_contestant_to_computer(
            contestant=student,
            computer=second,
            source="test",
        )

        roster_response, roster_timestamp = self.signed_request(
            "GET", reverse("ctrl.api.toilet.students")
        )
        self.assertEqual(
            roster_response.json(),
            {
                "students": [
                    {
                        "id": student.pk,
                        "userid": "alice",
                    }
                ]
            },
        )
        self.assert_signed_response(roster_response, roster_timestamp)

        layout_path = reverse(
            "ctrl.api.toilet.class_layout",
            args=[location.public_id],
        )
        layout_response, layout_timestamp = self.signed_request(
            "GET", layout_path
        )
        payload = layout_response.json()
        self.assertEqual(payload["class"]["grid_cols"], 5)
        self.assertEqual(
            [computer["machine_id"] for computer in payload["computers"]],
            [second.machine_id, first.machine_id],
        )
        self.assertEqual(
            payload["computers"][0]["student"],
            {
                "id": student.pk,
                "userid": "alice",
            },
        )
        self.assertEqual(payload["computers"][0]["grid_row"], 1)
        self.assertEqual(payload["computers"][0]["grid_col"], 1)
        self.assert_signed_response(layout_response, layout_timestamp)

    def test_missing_layout_is_a_signed_404(self):
        path = reverse(
            "ctrl.api.toilet.class_layout",
            args=["9d382b44-3dfd-4c52-bc3d-cadc34182ad1"],
        )
        response, timestamp = self.signed_request("GET", path)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "class_not_found"})
        self.assert_signed_response(response, timestamp)


class StudentAssignmentApiTests(ToiletApiTestCase):
    def setUp(self):
        super().setUp()
        self.path = reverse("ctrl.api.toilet.student_assignment")

    def assignment(self, username):
        response, timestamp = self.signed_request(
            "POST", self.path, {"userid": username}
        )
        self.assertEqual(response.status_code, 200)
        self.assert_signed_response(response, timestamp)
        return response.json()

    def test_missing_student(self):
        payload = self.assignment("missing")
        self.assertFalse(payload["found"])
        self.assertEqual(payload["anomalies"], [{"code": "student_not_found"}])

    def test_student_without_computers(self):
        Student.objects.create(userid="student")
        payload = self.assignment("student")
        self.assertEqual(payload["computers"], [])
        self.assertEqual(payload["classes"], [])
        self.assertEqual(payload["anomalies"], [{"code": "no_computers"}])

    def test_one_mapped_computer(self):
        location = Location.objects.create(name="101")
        computer = Computer.objects.create(
            machine_id="mid-1", name="PC 1", location=location
        )
        student = Student.objects.create(userid="student")
        assign_contestant_to_computer(
            contestant=student,
            computer=computer,
            source="test",
        )

        payload = self.assignment(" student ")
        self.assertEqual(payload["userid"], "student")
        self.assertEqual(
            payload["student"],
            {"id": student.pk, "userid": "student"},
        )
        self.assertEqual(payload["anomalies"], [])
        self.assertEqual(payload["classes"], [
            {
                "id": str(location.public_id),
                "name": "101",
                "sequence_num": 0,
                "grid_cols": None,
            }
        ])
        self.assertEqual(payload["computers"][0]["class"]["id"], str(location.public_id))

    def test_computer_without_class_is_flagged(self):
        computer = Computer.objects.create(machine_id="mid-1", name="PC 1")
        student = Student.objects.create(userid="student")
        assign_contestant_to_computer(
            contestant=student,
            computer=computer,
            source="test",
        )

        payload = self.assignment("student")
        self.assertEqual(len(payload["computers"]), 1)
        self.assertEqual(payload["classes"], [])
        self.assertEqual(
            payload["anomalies"],
            [{"code": "computer_without_class", "machine_ids": ["mid-1"]}],
        )


class ManagementUiTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.superuser = get_user_model().objects.create_superuser(
            username="root",
            password="Root-Test-Pass-94!",
            email="root@example.test",
        )
        self.normal_user = get_user_model().objects.create_user(
            username="normal", password="Normal-Test-Pass-94!"
        )

    def test_computer_class_reassignment_clears_grid_position(self):
        old = Location.objects.create(name="Old")
        new = Location.objects.create(name="New")
        computer = Computer.objects.create(
            machine_id="mid-1",
            name="PC 1",
            location=old,
            grid_row=3,
            grid_col=4,
        )
        path = reverse("ctrl.computer_edit", args=[computer.machine_id])
        self.client.force_login(self.superuser)
        response = self.client.post(
            path,
            {"name": "PC 1", "location": str(new.pk), "sequence_num": "2"},
        )
        self.assertEqual(response.status_code, 302)
        computer.refresh_from_db()
        self.assertEqual(computer.location, new)
        self.assertIsNone(computer.grid_row)
        self.assertIsNone(computer.grid_col)

        response = self.client.post(
            path,
            {"name": "PC 1", "location": "", "sequence_num": "2"},
        )
        self.assertEqual(response.status_code, 302)
        computer.refresh_from_db()
        self.assertIsNone(computer.location)

    def test_layout_mutations_persist_grid_size_and_positions(self):
        location = Location.objects.create(name="Layout class")
        computer = Computer.objects.create(
            machine_id="layout-mid",
            name="Layout PC",
            location=location,
        )
        edit_path = reverse("ctrl.location_edit_layout", args=[location.pk])
        save_path = reverse("ctrl.location_save_layout", args=[location.pk])
        payload = {
            "grid_cols": 8,
            "positions": [{"id": computer.pk, "row": 2, "col": 3}],
        }

        self.client.force_login(self.superuser)
        self.assertEqual(self.client.get(edit_path).status_code, 200)
        self.assertEqual(
            self.client.post(save_path, payload, content_type="application/json").status_code,
            200,
        )
        location.refresh_from_db()
        computer.refresh_from_db()
        self.assertEqual(location.grid_cols, 8)
        self.assertEqual((computer.grid_row, computer.grid_col), (2, 3))

    def test_layout_save_validates_complete_final_layout(self):
        location = Location.objects.create(name="Validated layout")
        first = Computer.objects.create(
            machine_id="validated-first",
            name="First",
            location=location,
        )
        second = Computer.objects.create(
            machine_id="validated-second",
            name="Second",
            location=location,
        )
        path = reverse("ctrl.location_save_layout", args=[location.pk])
        self.client.force_login(self.superuser)

        self.assertEqual(
            self.client.post(path, [], content_type="application/json").status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                path,
                {
                    "grid_cols": 4,
                    "positions": [{"id": first.pk, "row": 1, "col": 1}],
                },
                content_type="application/json",
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                path,
                {
                    "grid_cols": 4,
                    "positions": [
                        {"id": first.pk, "row": 1, "col": 1},
                        {"id": second.pk, "row": 1, "col": 1},
                    ],
                },
                content_type="application/json",
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                path,
                {
                    "grid_cols": 4,
                    "positions": [
                        {"id": first.pk, "row": 0, "col": 1},
                        {"id": second.pk, "row": None, "col": None},
                    ],
                },
                content_type="application/json",
            ).status_code,
            400,
        )

        response = self.client.post(
            path,
            {
                "grid_cols": 4,
                "positions": [
                    {"id": first.pk, "row": 1, "col": 1},
                    {"id": second.pk, "row": None, "col": None},
                ],
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((first.grid_row, first.grid_col), (1, 1))
        self.assertEqual((second.grid_row, second.grid_col), (None, None))

    def test_layout_save_can_atomically_swap_occupied_cells(self):
        location = Location.objects.create(name="Swap layout", grid_cols=2)
        first = Computer.objects.create(
            machine_id="swap-first",
            name="First",
            location=location,
            grid_row=1,
            grid_col=1,
        )
        second = Computer.objects.create(
            machine_id="swap-second",
            name="Second",
            location=location,
            grid_row=1,
            grid_col=2,
        )
        path = reverse("ctrl.location_save_layout", args=[location.pk])
        self.client.force_login(self.superuser)

        response = self.client.post(
            path,
            {
                "grid_cols": 2,
                "positions": [
                    {"id": first.pk, "row": 1, "col": 2},
                    {"id": second.pk, "row": 1, "col": 1},
                ],
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((first.grid_row, first.grid_col), (1, 2))
        self.assertEqual((second.grid_row, second.grid_col), (1, 1))

    def test_class_form_cannot_shrink_grid_past_placed_computers(self):
        location = Location.objects.create(name="Grid class", grid_cols=6)
        Computer.objects.create(
            machine_id="far-column",
            name="Far PC",
            location=location,
            grid_row=1,
            grid_col=6,
        )
        path = reverse("ctrl.location_edit", args=[location.pk])
        self.client.force_login(self.superuser)

        rejected = self.client.post(
            path,
            {"name": "Grid class", "sequence_num": 0, "grid_cols": 3},
        )
        self.assertEqual(rejected.status_code, 200)
        location.refresh_from_db()
        self.assertEqual(location.grid_cols, 6)

        accepted = self.client.post(
            path,
            {"name": "Grid class", "sequence_num": 0, "grid_cols": 8},
        )
        self.assertEqual(accepted.status_code, 302)
        location.refresh_from_db()
        self.assertEqual(location.grid_cols, 8)

    def test_student_save_assignment_and_anomaly_display(self):
        computer = Computer.objects.create(machine_id="mid-2", name="Backup")
        path = reverse("ctrl.student_new")
        self.client.force_login(self.superuser)
        response = self.client.post(path, {"userid": "student"})
        self.assertEqual(response.status_code, 302)
        student = Student.objects.get(userid="student")
        assignment_response = self.client.post(
            reverse("ctrl.assignment_new"),
            {
                "contestant": str(student.pk),
                "computer": str(computer.pk),
            },
        )
        self.assertEqual(assignment_response.status_code, 302)
        self.assertEqual(student.computer, computer)

        list_response = self.client.get(reverse("ctrl.student_list"))
        self.assertContains(list_response, "kompiuteris be klasės")

    def test_assignment_uses_a_separate_form_from_student_editing(self):
        computer = Computer.objects.create(machine_id="permission-pc", name="PC")
        self.client.force_login(self.superuser)

        self.assertEqual(
            self.client.post(
                reverse("ctrl.student_new"),
                {"userid": "student"},
            ).status_code,
            302,
        )
        student = Student.objects.get(userid="student")
        # Editing the contestant alone never creates a mapping.
        self.assertFalse(
            ContestantComputerAssignment.objects.filter(contestant=student).exists()
        )
        self.assertEqual(
            self.client.post(
                reverse("ctrl.assignment_new"),
                {"contestant": str(student.pk), "computer": str(computer.pk)},
            ).status_code,
            302,
        )
        self.assertEqual(student.computer, computer)

    def test_management_ui_requires_a_login_and_nothing_more(self):
        """Matches master: GUI views are gated by ``@login_required`` only."""

        location = Location.objects.create(name="Shared class")
        self.client.force_login(self.normal_user)
        for path in (
            reverse("ctrl.location_list"),
            reverse("ctrl.location_edit", args=[location.pk]),
            reverse("ctrl.student_list"),
            reverse("ctrl.student_new"),
            reverse("ctrl.assignment_list"),
            reverse("ctrl.unknown_computers"),
        ):
            self.assertEqual(self.client.get(path).status_code, 200, path)

        self.client.logout()
        anonymous = self.client.get(reverse("ctrl.student_list"))
        self.assertEqual(anonymous.status_code, 302)
        self.assertIn("/admin/login/", anonymous["Location"])

    def test_class_creation_and_unknown_computer_registration(self):
        new_class = reverse("ctrl.location_new")
        self.client.force_login(self.superuser)
        self.assertEqual(
            self.client.post(
                new_class, {"name": "Allowed", "sequence_num": 1}
            ).status_code,
            302,
        )
        location = Location.objects.get(name="Allowed")
        unknown = UnknownComputer.objects.create(machine_id="new-machine")
        register = reverse("ctrl.register_computer", args=[unknown.pk])
        self.assertEqual(
            self.client.post(
                register, {"name": "Managed PC", "location": location.pk}
            ).status_code,
            302,
        )
        self.assertTrue(Computer.objects.filter(machine_id="new-machine").exists())


class DirectContestantMigrationTests(TransactionTestCase):
    migrate_from = ("ctrl", "0011_task_presets")
    migrate_to = ("ctrl", "0012_contestant_computer_mapping")

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps

        Location = old_apps.get_model("ctrl", "Location")
        Computer = old_apps.get_model("ctrl", "Computer")
        first_location = Location.objects.create(name="Old class")
        second_location = Location.objects.create(name="Other class")
        Computer.objects.create(
            machine_id="migration-mid",
            name="Migration PC",
            location=first_location,
        )
        Computer.objects.create(
            machine_id="ambiguous-migration-mid",
            name="Ambiguous Migration PC",
            location=second_location,
        )

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        self.apps = executor.loader.project_state([self.migrate_to]).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_current_production_schema_migrates_directly_to_final_models(self):
        Location = self.apps.get_model("ctrl", "Location")
        Student = self.apps.get_model("ctrl", "Student")
        Computer = self.apps.get_model("ctrl", "Computer")
        Assignment = self.apps.get_model(
            "ctrl", "ContestantComputerAssignment"
        )
        Event = self.apps.get_model(
            "ctrl", "ContestantComputerAssignmentEvent"
        )
        computer = Computer.objects.get(machine_id="migration-mid")
        ambiguous_computer = Computer.objects.get(
            machine_id="ambiguous-migration-mid"
        )

        self.assertEqual(
            {field.name for field in Student._meta.fields},
            {"id", "userid"},
        )
        for absent_field in ("name", "cms_username", "computers"):
            with self.assertRaises(FieldDoesNotExist):
                Student._meta.get_field(absent_field)
        self.assertFalse(Assignment.objects.exists())
        self.assertFalse(Event.objects.exists())
        self.assertEqual(computer.name, "Migration PC")
        self.assertEqual(ambiguous_computer.name, "Ambiguous Migration PC")
        self.assertTrue(Computer._meta.get_field("machine_id").unique)
        public_ids = list(Location.objects.values_list("public_id", flat=True))
        self.assertEqual(len(public_ids), 2)
        self.assertEqual(len(set(public_ids)), 2)
        self.assertNotIn(None, public_ids)


class DuplicateMachineMigrationTests(TransactionTestCase):
    migrate_from = ("ctrl", "0011_task_presets")
    migrate_to = ("ctrl", "0012_contestant_computer_mapping")

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        self.old_apps = executor.loader.project_state([self.migrate_from]).apps

    def tearDown(self):
        Computer = self.old_apps.get_model("ctrl", "Computer")
        duplicates = Computer.objects.filter(machine_id="duplicate-mid").order_by("pk")
        for computer in list(duplicates)[1:]:
            computer.delete()
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_duplicate_preflight_aborts_with_actionable_values(self):
        Computer = self.old_apps.get_model("ctrl", "Computer")
        Computer.objects.create(machine_id="duplicate-mid", name="First")
        Computer.objects.create(machine_id="duplicate-mid", name="Second")

        executor = MigrationExecutor(connection)
        with self.assertRaisesRegex(RuntimeError, "duplicate-mid.*2 rows"):
            executor.migrate([self.migrate_to])

from io import StringIO
import os
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import NoReverseMatch, reverse
from tablib import Dataset

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
)
from .resources import (
    ComputerResource,
    ContestantComputerAssignmentResource,
    ContestantResource,
)


class StudentCsvImportTests(TestCase):
    def csv_file(self, content):
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            suffix=".csv",
            delete=False,
        )
        self.addCleanup(lambda: os.unlink(handle.name))
        with handle:
            handle.write(content)
        return handle.name

    def test_import_upserts_identifier_only_contestants(self):
        path = self.csv_file("identifier\nalice\nbob\n")
        output = StringIO()

        call_command("import_students_csv", path, stdout=output)
        call_command("import_students_csv", path, stdout=StringIO())

        self.assertEqual(
            list(Student.objects.values_list("userid", flat=True)),
            ["alice", "bob"],
        )
        self.assertIn("Imported 2 contestants", output.getvalue())

    def test_dry_run_and_invalid_files_do_not_write(self):
        valid = self.csv_file("identifier\nalice\n")
        output = StringIO()
        call_command(
            "import_students_csv",
            valid,
            dry_run=True,
            stdout=output,
        )
        self.assertFalse(Student.objects.exists())
        self.assertIn("no changes written", output.getvalue())

        invalid_files = (
            "userid\nalice\n",
            "identifier,name\nalice,Alice\n",
            "identifier\nalice\nalice\n",
            "identifier\n alice\n",
            "identifier\n",
        )
        for content in invalid_files:
            with self.subTest(content=content):
                with self.assertRaises(CommandError):
                    call_command(
                        "import_students_csv",
                        self.csv_file(content),
                    )
        self.assertFalse(Student.objects.exists())


class AdminImportResourceTests(TestCase):
    def dataset(self, headers, *rows):
        return Dataset(*rows, headers=headers)

    def import_data(self, resource, dataset):
        return resource.import_data(
            dataset,
            dry_run=False,
            rollback_on_validation_errors=True,
        )

    def test_contestant_resource_is_identifier_only_and_idempotent(self):
        dataset = self.dataset(["identifier"], ["alice"], ["bob"])
        first = self.import_data(ContestantResource(), dataset)
        second = self.import_data(ContestantResource(), dataset)

        self.assertFalse(first.has_errors())
        self.assertFalse(first.has_validation_errors())
        self.assertFalse(second.has_errors())
        self.assertEqual(Student.objects.count(), 2)

        invalid = self.dataset(
            ["identifier", "name"],
            ["charlie", "Charlie Example"],
        )
        result = self.import_data(ContestantResource(), invalid)
        self.assertTrue(result.has_errors())
        self.assertFalse(Student.objects.filter(userid="charlie").exists())

    def test_computer_resource_creates_updates_and_is_idempotent(self):
        location = Location.objects.create(name="Room A")
        headers = ["machine_id", "name", "class_id", "sequence_num"]
        initial = self.dataset(
            headers,
            ["pc-01", "Main PC", str(location.public_id), "2"],
            ["pc-02", "Spare PC", "", "5"],
        )

        first = self.import_data(ComputerResource(), initial)
        repeated = self.import_data(ComputerResource(), initial)

        self.assertFalse(first.has_errors())
        self.assertFalse(first.has_validation_errors())
        self.assertFalse(repeated.has_errors())
        self.assertFalse(repeated.has_validation_errors())
        self.assertEqual(Computer.objects.count(), 2)
        assigned = Computer.objects.get(machine_id="pc-01")
        unassigned = Computer.objects.get(machine_id="pc-02")
        self.assertEqual(assigned.name, "Main PC")
        self.assertEqual(assigned.location, location)
        self.assertEqual(assigned.sequence_num, 2)
        self.assertIsNone(unassigned.location)
        self.assertEqual(unassigned.sequence_num, 5)

        updated = self.dataset(
            headers,
            ["pc-01", "Renamed PC", "", "-3"],
        )
        result = self.import_data(ComputerResource(), updated)

        self.assertFalse(result.has_errors())
        self.assertFalse(result.has_validation_errors())
        assigned.refresh_from_db()
        self.assertEqual(assigned.name, "Renamed PC")
        self.assertIsNone(assigned.location)
        self.assertEqual(assigned.sequence_num, -3)
        self.assertEqual(Computer.objects.count(), 2)

    def test_computer_import_preserves_mapping_and_clears_moved_layout(self):
        first_location = Location.objects.create(name="Room A", grid_cols=6)
        second_location = Location.objects.create(name="Room B", grid_cols=6)
        computer = Computer.objects.create(
            machine_id="pc-01",
            name="Main PC",
            location=first_location,
            sequence_num=1,
            grid_row=2,
            grid_col=3,
        )
        contestant = Student.objects.create(userid="alice")
        assignment, _ = assign_contestant_to_computer(
            contestant=contestant,
            computer=computer,
            source="test",
        )
        event_count = ContestantComputerAssignmentEvent.objects.count()
        dataset = self.dataset(
            ["machine_id", "name", "class_id", "sequence_num"],
            [
                "pc-01",
                "Moved PC",
                str(second_location.public_id),
                "9",
            ],
        )

        result = self.import_data(ComputerResource(), dataset)

        self.assertFalse(result.has_errors())
        self.assertFalse(result.has_validation_errors())
        computer.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(computer.name, "Moved PC")
        self.assertEqual(computer.location, second_location)
        self.assertEqual(computer.sequence_num, 9)
        self.assertIsNone(computer.grid_row)
        self.assertIsNone(computer.grid_col)
        self.assertEqual(assignment.contestant, contestant)
        self.assertEqual(assignment.computer, computer)
        self.assertEqual(
            ContestantComputerAssignmentEvent.objects.count(),
            event_count,
        )

    def test_computer_import_rejects_unknown_classes_and_duplicates_atomically(self):
        headers = ["machine_id", "name", "class_id", "sequence_num"]
        unknown_class = self.dataset(
            headers,
            ["pc-valid", "Valid PC", "", "1"],
            [
                "pc-unknown",
                "Unknown room",
                "00000000-0000-0000-0000-000000000001",
                "2",
            ],
        )

        result = self.import_data(ComputerResource(), unknown_class)

        self.assertTrue(result.has_errors() or result.has_validation_errors())
        self.assertFalse(Computer.objects.exists())

        duplicate = self.dataset(
            headers,
            ["pc-duplicate", "First value", "", "1"],
            ["pc-duplicate", "Second value", "", "2"],
        )
        result = self.import_data(ComputerResource(), duplicate)

        self.assertTrue(result.has_errors() or result.has_validation_errors())
        self.assertFalse(Computer.objects.exists())

    def test_computer_import_rejects_bad_headers_and_values_atomically(self):
        headers = ["machine_id", "name", "class_id", "sequence_num"]
        invalid_datasets = [
            self.dataset(
                ["machine_id", "name", "sequence_num"],
                ["pc-01", "Missing class header", "1"],
            ),
            self.dataset(
                headers,
                ["pc-valid", "Valid PC", "", "1"],
                [" pc-invalid", "Bad identifier", "", "2"],
            ),
            self.dataset(
                headers,
                ["pc-valid", "Valid PC", "", "1"],
                ["pc-invalid", "", "", "2"],
            ),
            self.dataset(
                headers,
                ["pc-valid", "Valid PC", "", "1"],
                ["pc-invalid", "x" * 33, "", "2"],
            ),
            self.dataset(
                headers,
                ["pc-valid", "Valid PC", "", "1"],
                ["pc-invalid", "Bad sequence", "", "not-an-integer"],
            ),
            self.dataset(
                headers,
                ["pc-valid", "Valid PC", "", "1"],
                ["pc-invalid", "Bad class", "not-a-uuid", "2"],
            ),
        ]

        for dataset in invalid_datasets:
            with self.subTest(headers=dataset.headers, rows=dataset.dict):
                result = self.import_data(ComputerResource(), dataset)
                self.assertTrue(
                    result.has_errors() or result.has_validation_errors()
                )
                self.assertFalse(Computer.objects.exists())

    def result_error_text(self, result):
        messages = [
            str(getattr(error, "error", error))
            for error in result.base_errors
        ]
        for row in result.rows:
            messages.extend(
                str(getattr(error, "error", error))
                for error in row.errors
            )
            validation_error = getattr(row, "validation_error", None)
            if validation_error is not None:
                messages.append(str(validation_error))
        return " ".join(messages)

    def test_mapping_import_uses_computer_names_and_records_history(self):
        contestant = Student.objects.create(userid="alice")
        computer = Computer.objects.create(
            machine_id="opaque-machine-01",
            name="Seat 01",
        )
        dataset = self.dataset(
            ["contestant_identifier", "computer_name"],
            ["alice", "Seat 01"],
        )

        first = self.import_data(
            ContestantComputerAssignmentResource(),
            dataset,
        )
        second = self.import_data(
            ContestantComputerAssignmentResource(),
            dataset,
        )

        self.assertFalse(first.has_errors())
        self.assertFalse(first.has_validation_errors())
        self.assertFalse(second.has_errors())
        assignment = ContestantComputerAssignment.objects.get()
        self.assertEqual(assignment.contestant, contestant)
        self.assertEqual(assignment.computer, computer)
        self.assertEqual(assignment.source, "admin_import")
        event = ContestantComputerAssignmentEvent.objects.get()
        self.assertEqual(event.action, "assigned")
        self.assertEqual(event.assignment_id_snapshot, assignment.pk)
        self.assertEqual(event.contestant_id_snapshot, contestant.pk)
        self.assertEqual(event.computer_id_snapshot, computer.pk)
        self.assertEqual(event.computer_identifier, "opaque-machine-01")

    def test_mapping_import_rejects_name_lookup_errors_atomically(self):
        alice = Student.objects.create(userid="alice")
        bob = Student.objects.create(userid="bob")
        known = Computer.objects.create(
            machine_id="pc-known",
            name="Known seat",
        )
        Computer.objects.create(
            machine_id="pc-duplicate-a",
            name="Duplicate seat",
        )
        Computer.objects.create(
            machine_id="pc-duplicate-b",
            name="Duplicate seat",
        )
        invalid_datasets = [
            (
                self.dataset(
                    ["contestant_identifier", "computer_machine_id"],
                    ["alice", known.machine_id],
                ),
                "computer_name",
            ),
            (
                self.dataset(
                    ["contestant_identifier", "computer_name"],
                    ["alice", "Missing seat"],
                ),
                "Missing seat",
            ),
            (
                self.dataset(
                    ["contestant_identifier", "computer_name"],
                    ["alice", known.name],
                    ["bob", known.name],
                ),
                known.name,
            ),
            (
                self.dataset(
                    ["contestant_identifier", "computer_name"],
                    ["alice", "Duplicate seat"],
                ),
                "Duplicate seat",
            ),
        ]

        for dataset, expected_message in invalid_datasets:
            with self.subTest(
                rows=dataset.dict,
                expected_message=expected_message,
            ):
                result = self.import_data(
                    ContestantComputerAssignmentResource(),
                    dataset,
                )
                self.assertTrue(
                    result.has_errors() or result.has_validation_errors()
                )
                self.assertIn(
                    expected_message,
                    self.result_error_text(result),
                )
                self.assertFalse(
                    ContestantComputerAssignment.objects.exists()
                )
                self.assertFalse(
                    ContestantComputerAssignmentEvent.objects.exists()
                )

    def test_mapping_import_rejects_conflicts_atomically(self):
        alice = Student.objects.create(userid="alice")
        bob = Student.objects.create(userid="bob")
        charlie = Student.objects.create(userid="charlie")
        first = Computer.objects.create(machine_id="pc-01", name="PC 01")
        second = Computer.objects.create(machine_id="pc-02", name="PC 02")
        third = Computer.objects.create(machine_id="pc-03", name="PC 03")
        assign_contestant_to_computer(
            contestant=alice,
            computer=first,
            source="test",
        )
        before_events = ContestantComputerAssignmentEvent.objects.count()
        conflict_datasets = [
            self.dataset(
                ["contestant_identifier", "computer_name"],
                ["bob", second.name],
                ["alice", third.name],
            ),
            self.dataset(
                ["contestant_identifier", "computer_name"],
                ["bob", second.name],
                ["charlie", first.name],
            ),
        ]

        for dataset in conflict_datasets:
            with self.subTest(rows=dataset.dict):
                result = self.import_data(
                    ContestantComputerAssignmentResource(),
                    dataset,
                )

                self.assertTrue(
                    result.has_errors() or result.has_validation_errors()
                )
                self.assertIn(
                    "Remove that mapping before",
                    self.result_error_text(result),
                )
                self.assertEqual(
                    ContestantComputerAssignment.objects.count(),
                    1,
                )
                self.assertFalse(
                    ContestantComputerAssignment.objects.filter(
                        contestant__in=[bob, charlie]
                    ).exists()
                )
                self.assertEqual(
                    ContestantComputerAssignmentEvent.objects.count(),
                    before_events,
                )
                self.assertEqual(
                    ContestantComputerAssignment.objects.get().computer,
                    first,
                )

    def test_explicit_remove_then_reassign_keeps_clear_history(self):
        contestant = Student.objects.create(userid="alice")
        first = Computer.objects.create(machine_id="pc-01", name="PC 01")
        second = Computer.objects.create(machine_id="pc-02", name="PC 02")
        assignment, _ = assign_contestant_to_computer(
            contestant=contestant,
            computer=first,
            source="test",
        )
        remove_contestant_computer_assignment(
            assignment=assignment,
            source="test_remove",
        )

        dataset = self.dataset(
            ["contestant_identifier", "computer_name"],
            ["alice", second.name],
        )
        result = self.import_data(
            ContestantComputerAssignmentResource(),
            dataset,
        )

        self.assertFalse(result.has_errors())
        self.assertEqual(
            ContestantComputerAssignment.objects.get().computer,
            second,
        )
        events = list(
            ContestantComputerAssignmentEvent.objects.values_list(
                "action", "computer_identifier"
            )
        )
        self.assertEqual(
            events,
            [
                ("assigned", "pc-01"),
                ("removed", "pc-01"),
                ("assigned", "pc-02"),
            ],
        )


class AdminImportScopeTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser(
            "admin",
            "admin@example.test",
            "Admin-Test-Pass-94!",
        )
        self.client.force_login(self.admin)

    def test_contestants_computers_and_mappings_expose_import(self):
        self.assertEqual(
            self.client.get(reverse("admin:ctrl_student_import")).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(reverse("admin:ctrl_computer_import")).status_code,
            200,
        )
        mapping_import = self.client.get(
            reverse(
                "admin:ctrl_contestantcomputerassignment_import"
            )
        )
        self.assertEqual(mapping_import.status_code, 200)
        self.assertContains(mapping_import, "computer_name")
        self.assertNotContains(mapping_import, "computer_machine_id")
        with self.assertRaises(NoReverseMatch):
            reverse("admin:ctrl_location_import")
        with self.assertRaises(NoReverseMatch):
            reverse(
                "admin:ctrl_contestantcomputerassignmentevent_import"
            )

    def test_computer_import_requires_add_and_change_permissions(self):
        path = reverse("admin:ctrl_computer_import")
        staff = get_user_model().objects.create_user(
            "computer-importer",
            password="Computer-Import-Test-94!",
            is_staff=True,
        )
        add_permission = Permission.objects.get(
            content_type__app_label="ctrl",
            codename="add_computer",
        )
        change_permission = Permission.objects.get(
            content_type__app_label="ctrl",
            codename="change_computer",
        )
        self.client.force_login(staff)

        staff.user_permissions.add(add_permission)
        self.assertEqual(self.client.get(path).status_code, 403)

        staff.user_permissions.clear()
        staff.user_permissions.add(change_permission)
        self.assertEqual(self.client.get(path).status_code, 403)

        staff.user_permissions.add(add_permission)
        self.assertEqual(self.client.get(path).status_code, 200)

    def test_admin_mapping_creation_uses_service_and_records_actor(self):
        contestant = Student.objects.create(userid="alice")
        computer = Computer.objects.create(machine_id="pc-01", name="PC 01")

        response = self.client.post(
            reverse("admin:ctrl_contestantcomputerassignment_add"),
            {
                "contestant": str(contestant.pk),
                "computer": str(computer.pk),
                "_save": "Save",
            },
        )

        self.assertEqual(response.status_code, 302)
        assignment = ContestantComputerAssignment.objects.get()
        self.assertEqual(assignment.source, "django_admin")
        event = ContestantComputerAssignmentEvent.objects.get()
        self.assertEqual(event.actor_identifier, self.admin.get_username())

    def test_concurrent_admin_conflict_returns_clear_message(self):
        contestant = Student.objects.create(userid="alice")
        computer = Computer.objects.create(machine_id="pc-01", name="PC 01")
        path = reverse("admin:ctrl_contestantcomputerassignment_add")

        with patch(
            "ctrl.admin.assign_contestant_to_computer",
            side_effect=AssignmentConflict(
                "Remove that mapping before assigning a new one."
            ),
        ):
            response = self.client.post(
                path,
                {
                    "contestant": str(contestant.pk),
                    "computer": str(computer.pk),
                    "_save": "Save",
                },
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Remove that mapping before assigning a new one.",
        )
        self.assertFalse(ContestantComputerAssignment.objects.exists())

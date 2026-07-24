"""CSV resources exposed by the targeted Django admin import UI."""

import uuid

from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils.encoding import force_str
from import_export import fields, resources, widgets

from .assignments import assign_contestant_to_computer
from .models import (
    Computer,
    ContestantComputerAssignment,
    Location,
    Student,
)


def _require_exact_headers(dataset, expected):
    actual = tuple(dataset.headers or ())
    if actual != expected:
        raise ValidationError(
            "CSV headers must be exactly: " + ",".join(expected)
        )


def _required_identifier(value, *, column, max_length):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValidationError(
            {column: f"{column} must be a non-empty value without outer whitespace."}
        )
    if len(value) > max_length:
        raise ValidationError(
            {column: f"{column} must contain at most {max_length} characters."}
        )
    return value


def _required_integer(value, *, column, row_number):
    if isinstance(value, bool):
        value = None
    if isinstance(value, str):
        if not value or value != value.strip():
            value = None
        else:
            try:
                return int(value)
            except ValueError:
                value = None
    elif isinstance(value, int):
        return value
    raise ValidationError(
        f"Line {row_number}: {column} must be an integer."
    )


def _optional_uuid(value, *, column, row_number):
    if value in (None, ""):
        return None
    if not isinstance(value, str) or value != value.strip():
        raise ValidationError(
            f"Line {row_number}: {column} must be blank or a valid UUID."
        )
    try:
        return uuid.UUID(value)
    except ValueError as error:
        raise ValidationError(
            f"Line {row_number}: {column} must be blank or a valid UUID."
        ) from error


class ContestantResource(resources.ModelResource):
    identifier = fields.Field(attribute="userid", column_name="identifier")

    class Meta:
        model = Student
        fields = ("identifier",)
        import_id_fields = ("identifier",)
        clean_model_instances = True
        skip_unchanged = True
        report_skipped = True
        use_bulk = False
        use_transactions = True

    def before_import(self, dataset, **kwargs):
        _require_exact_headers(dataset, ("identifier",))
        seen = set()
        for row_number, row in enumerate(dataset.dict, start=2):
            identifier = _required_identifier(
                row.get("identifier"),
                column="identifier",
                max_length=64,
            )
            if identifier in seen:
                raise ValidationError(
                    f'Line {row_number}: duplicate contestant identifier '
                    f'"{identifier}".'
                )
            seen.add(identifier)


class ComputerResource(resources.ModelResource):
    machine_id = fields.Field(
        attribute="machine_id",
        column_name="machine_id",
    )
    name = fields.Field(attribute="name", column_name="name")
    class_id = fields.Field(
        attribute="location",
        column_name="class_id",
        widget=widgets.ForeignKeyWidget(Location, field="public_id"),
    )
    sequence_num = fields.Field(
        attribute="sequence_num",
        column_name="sequence_num",
        widget=widgets.IntegerWidget(),
    )

    class Meta:
        model = Computer
        fields = ("machine_id", "name", "class_id", "sequence_num")
        import_id_fields = ("machine_id",)
        clean_model_instances = True
        skip_unchanged = True
        report_skipped = True
        use_bulk = False
        use_transactions = True

    def before_import(self, dataset, **kwargs):
        expected = ("machine_id", "name", "class_id", "sequence_num")
        _require_exact_headers(dataset, expected)

        seen_machine_ids = set()
        class_ids = set()
        for row_number, row in enumerate(dataset.dict, start=2):
            machine_id = _required_identifier(
                row.get("machine_id"),
                column="machine_id",
                max_length=40,
            )
            _required_identifier(
                row.get("name"),
                column="name",
                max_length=32,
            )
            class_id = _optional_uuid(
                row.get("class_id"),
                column="class_id",
                row_number=row_number,
            )
            _required_integer(
                row.get("sequence_num"),
                column="sequence_num",
                row_number=row_number,
            )
            if machine_id in seen_machine_ids:
                raise ValidationError(
                    f'Line {row_number}: duplicate computer machine ID '
                    f'"{machine_id}".'
                )
            seen_machine_ids.add(machine_id)
            if class_id is not None:
                class_ids.add(class_id)

        known_class_ids = set(
            Location.objects.filter(public_id__in=class_ids).values_list(
                "public_id", flat=True
            )
        )
        unknown_class_ids = sorted(
            (str(item) for item in class_ids - known_class_ids)
        )
        if unknown_class_ids:
            raise ValidationError(
                "Unknown class IDs: " + ", ".join(unknown_class_ids)
            )


class UniqueComputerNameWidget(widgets.ForeignKeyWidget):
    """Resolve an operator-facing name only when it identifies one computer."""

    def clean(self, value, row=None, **kwargs):
        if value in (None, ""):
            return None
        matches = list(
            self.get_queryset(value, row, **kwargs)
            .filter(name=value)
            .order_by("machine_id", "pk")
        )
        if not matches:
            raise ValidationError(
                {"computer_name": f'Unknown computer name "{value}".'}
            )
        if len(matches) != 1:
            machine_ids = ", ".join(item.machine_id for item in matches)
            raise ValidationError(
                {
                    "computer_name": (
                        f'Computer name "{value}" is ambiguous; matching '
                        f"machine IDs: {machine_ids}. Rename computers so the "
                        "imported name is unique."
                    )
                }
            )
        return matches[0]


class ContestantComputerAssignmentResource(resources.ModelResource):
    contestant_identifier = fields.Field(
        attribute="contestant",
        column_name="contestant_identifier",
        widget=widgets.ForeignKeyWidget(Student, field="userid"),
    )
    computer_name = fields.Field(
        attribute="computer",
        column_name="computer_name",
        widget=UniqueComputerNameWidget(Computer, field="name"),
    )

    class Meta:
        model = ContestantComputerAssignment
        fields = ("contestant_identifier", "computer_name")
        import_id_fields = ("contestant_identifier", "computer_name")
        clean_model_instances = True
        skip_unchanged = True
        report_skipped = True
        use_bulk = False
        use_transactions = True

    def before_import(self, dataset, **kwargs):
        expected = ("contestant_identifier", "computer_name")
        _require_exact_headers(dataset, expected)

        prepared = []
        seen_contestants = {}
        seen_computers = {}
        for row_number, row in enumerate(dataset.dict, start=2):
            contestant_identifier = _required_identifier(
                row.get("contestant_identifier"),
                column="contestant_identifier",
                max_length=64,
            )
            computer_name = _required_identifier(
                row.get("computer_name"),
                column="computer_name",
                max_length=32,
            )
            if contestant_identifier in seen_contestants:
                raise ValidationError(
                    f'Line {row_number}: contestant "{contestant_identifier}" '
                    "appears more than once; each contestant can have only one "
                    "computer."
                )
            if computer_name in seen_computers:
                raise ValidationError(
                    f'Line {row_number}: computer "{computer_name}" '
                    "appears more than once; each computer can have only one "
                    "contestant."
                )
            seen_contestants[contestant_identifier] = row_number
            seen_computers[computer_name] = row_number
            prepared.append(
                (row_number, contestant_identifier, computer_name)
            )

        known_contestants = set(
            Student.objects.filter(userid__in=seen_contestants).values_list(
                "userid", flat=True
            )
        )
        unknown_contestants = sorted(set(seen_contestants) - known_contestants)
        if unknown_contestants:
            raise ValidationError(
                "Unknown contestant identifiers: "
                + ", ".join(force_str(item) for item in unknown_contestants)
            )

        computer_candidates = {}
        for item in (
            Computer.objects.filter(name__in=seen_computers)
            .values("name", "machine_id")
            .order_by("name", "machine_id")
        ):
            computer_candidates.setdefault(item["name"], []).append(
                item["machine_id"]
            )
        unknown_computers = sorted(
            set(seen_computers) - set(computer_candidates)
        )
        if unknown_computers:
            details = "; ".join(
                f'line {seen_computers[name]}: "{name}"'
                for name in unknown_computers
            )
            raise ValidationError(
                "Unknown computer names: " + details
            )

        ambiguous_computers = sorted(
            (
                name,
                machine_ids,
            )
            for name, machine_ids in computer_candidates.items()
            if len(machine_ids) != 1
        )
        if ambiguous_computers:
            details = "; ".join(
                (
                    f'line {seen_computers[name]}: "{name}" matches machine '
                    f'IDs {", ".join(machine_ids)}'
                )
                for name, machine_ids in ambiguous_computers
            )
            raise ValidationError(
                "Computer names must identify exactly one computer; "
                + details
                + ". Rename computers so each imported name is unique."
            )

        existing = list(
            ContestantComputerAssignment.objects.select_related(
                "contestant", "computer"
            ).filter(
                Q(contestant__userid__in=seen_contestants)
                | Q(computer__name__in=seen_computers)
            )
        )
        by_contestant = {
            item.contestant.userid: item for item in existing
        }
        by_computer = {
            item.computer.name: item for item in existing
        }
        for row_number, contestant_identifier, computer_name in prepared:
            contestant_mapping = by_contestant.get(contestant_identifier)
            computer_mapping = by_computer.get(computer_name)
            if (
                contestant_mapping is not None
                and contestant_mapping.computer.name != computer_name
            ):
                raise ValidationError(
                    f'Line {row_number}: contestant "{contestant_identifier}" '
                    f'is already assigned to computer '
                    f'"{contestant_mapping.computer.name}". Remove that '
                    "mapping before assigning a new one."
                )
            if (
                computer_mapping is not None
                and computer_mapping.contestant.userid
                != contestant_identifier
            ):
                raise ValidationError(
                    f'Line {row_number}: computer "{computer_name}" is '
                    f'already assigned to contestant '
                    f'"{computer_mapping.contestant.userid}". Remove that '
                    "mapping before assigning a new one."
                )

    def before_save_instance(self, instance, row, **kwargs):
        user = kwargs.get("user")
        instance._import_actor_identifier = (
            user.get_username() if user is not None else ""
        )

    def do_instance_save(self, instance, is_create):
        saved, _created = assign_contestant_to_computer(
            contestant=instance.contestant,
            computer=instance.computer,
            source="admin_import",
            actor_identifier=getattr(
                instance, "_import_actor_identifier", ""
            ),
        )
        instance.pk = saved.pk
        instance.assigned_at = saved.assigned_at
        instance.source = saved.source
        instance._state.adding = False
        instance._state.db = saved._state.db

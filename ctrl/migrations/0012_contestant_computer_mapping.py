import uuid

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import migrations, models
from django.db.models import Max, Q
import django.db.models.deletion


def populate_location_public_ids(apps, schema_editor):
    Location = apps.get_model("ctrl", "Location")
    locations = Location.objects.using(schema_editor.connection.alias)
    for location in locations.filter(public_id__isnull=True).iterator():
        location.public_id = uuid.uuid4()
        location.save(update_fields=["public_id"])


def reject_duplicate_machine_ids(apps, schema_editor):
    Computer = apps.get_model("ctrl", "Computer")
    computers = Computer.objects.using(schema_editor.connection.alias)
    duplicates = list(
        computers.values("machine_id")
        .annotate(row_count=models.Count("id"))
        .filter(row_count__gt=1)
        .order_by("machine_id")
    )
    if duplicates:
        values = ", ".join(
            f"{row['machine_id']!r} ({row['row_count']} rows)"
            for row in duplicates
        )
        raise RuntimeError(
            "Cannot make Computer.machine_id unique; resolve duplicate machine "
            "IDs first: " + values
        )


def normalize_invalid_layouts(apps, schema_editor):
    Location = apps.get_model("ctrl", "Location")
    Computer = apps.get_model("ctrl", "Computer")
    database_alias = schema_editor.connection.alias
    locations = Location.objects.using(database_alias)
    computers = Computer.objects.using(database_alias)

    locations.filter(grid_cols__lte=0).update(grid_cols=None)
    locations.filter(grid_cols__gt=30).update(grid_cols=30)
    computers.filter(
        Q(grid_row__lte=0)
        | Q(grid_row__gt=30)
        | Q(grid_col__lte=0)
        | Q(grid_col__gt=30)
        | Q(grid_row__isnull=True, grid_col__isnull=False)
        | Q(grid_row__isnull=False, grid_col__isnull=True)
    ).update(grid_row=None, grid_col=None)

    for location in locations.filter(grid_cols__isnull=True).iterator():
        maximum_column = computers.filter(
            location_id=location.pk
        ).aggregate(maximum=Max("grid_col"))["maximum"]
        if maximum_column is not None:
            location.grid_cols = maximum_column
            location.save(update_fields=["grid_cols"])
    for location in locations.exclude(grid_cols=None).iterator():
        computers.filter(
            location_id=location.pk,
            grid_col__gt=location.grid_cols,
        ).update(grid_row=None, grid_col=None)

    duplicates = (
        computers.filter(
            location_id__isnull=False,
            grid_row__isnull=False,
            grid_col__isnull=False,
        )
        .values("location_id", "grid_row", "grid_col")
        .annotate(row_count=models.Count("id"))
        .filter(row_count__gt=1)
    )
    for duplicate in duplicates.iterator():
        duplicate_ids = list(
            computers.filter(
                location_id=duplicate["location_id"],
                grid_row=duplicate["grid_row"],
                grid_col=duplicate["grid_col"],
            )
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        computers.filter(pk__in=duplicate_ids[1:]).update(
            grid_row=None,
            grid_col=None,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("ctrl", "0011_task_presets"),
    ]

    operations = [
        migrations.RunPython(
            reject_duplicate_machine_ids,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="computer",
            name="machine_id",
            field=models.CharField(max_length=40, unique=True),
        ),
        migrations.AddField(
            model_name="location",
            name="public_id",
            field=models.UUIDField(editable=False, null=True),
        ),
        migrations.RunPython(
            populate_location_public_ids,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="location",
            name="public_id",
            field=models.UUIDField(
                default=uuid.uuid4,
                editable=False,
                unique=True,
            ),
        ),
        migrations.CreateModel(
            name="Student",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "userid",
                    models.CharField(
                        db_index=True,
                        max_length=64,
                        unique=True,
                    ),
                ),
            ],
            options={
                "ordering": ["userid"],
                "verbose_name": "contestant",
                "verbose_name_plural": "contestants",
            },
        ),
        migrations.CreateModel(
            name="ContestantComputerAssignmentEvent",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("assigned", "Assigned"),
                            ("removed", "Removed"),
                        ],
                        max_length=32,
                    ),
                ),
                (
                    "assignment_id_snapshot",
                    models.BigIntegerField(),
                ),
                ("contestant_id_snapshot", models.BigIntegerField()),
                ("computer_id_snapshot", models.BigIntegerField()),
                ("contestant_identifier", models.CharField(max_length=64)),
                ("computer_identifier", models.CharField(max_length=40)),
                ("occurred_at", models.DateTimeField(auto_now_add=True)),
                ("source", models.CharField(default="manual", max_length=64)),
                (
                    "actor_identifier",
                    models.CharField(blank=True, default="", max_length=150),
                ),
                ("message", models.TextField()),
            ],
            options={
                "verbose_name": (
                    "contestant-computer mapping history event"
                ),
                "verbose_name_plural": (
                    "contestant-computer mapping history"
                ),
                "ordering": ["occurred_at", "pk"],
            },
        ),
        migrations.CreateModel(
            name="ContestantComputerAssignment",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("assigned_at", models.DateTimeField(auto_now_add=True)),
                (
                    "source",
                    models.CharField(
                        default="manual",
                        editable=False,
                        max_length=64,
                    ),
                ),
                (
                    "computer",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="contestant_assignment",
                        to="ctrl.computer",
                    ),
                ),
                (
                    "contestant",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="computer_assignment",
                        to="ctrl.student",
                    ),
                ),
            ],
            options={
                "ordering": [
                    "contestant__userid",
                    "computer__machine_id",
                ],
                "verbose_name": "contestant-computer mapping",
                "verbose_name_plural": "contestant-computer mappings",
            },
        ),
        migrations.RunPython(
            normalize_invalid_layouts,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="location",
            name="grid_cols",
            field=models.PositiveIntegerField(
                blank=True,
                null=True,
                validators=[
                    MinValueValidator(1),
                    MaxValueValidator(30),
                ],
            ),
        ),
        migrations.AlterField(
            model_name="computer",
            name="grid_row",
            field=models.PositiveIntegerField(
                blank=True,
                null=True,
                validators=[
                    MinValueValidator(1),
                    MaxValueValidator(30),
                ],
            ),
        ),
        migrations.AlterField(
            model_name="computer",
            name="grid_col",
            field=models.PositiveIntegerField(
                blank=True,
                null=True,
                validators=[
                    MinValueValidator(1),
                    MaxValueValidator(30),
                ],
            ),
        ),
        migrations.AddConstraint(
            model_name="location",
            constraint=models.CheckConstraint(
                condition=Q(grid_cols__isnull=True)
                | Q(grid_cols__gte=1, grid_cols__lte=30),
                name="ctrl_location_grid_cols_gt_zero",
            ),
        ),
        migrations.AddConstraint(
            model_name="computer",
            constraint=models.CheckConstraint(
                condition=(
                    Q(grid_row__isnull=True, grid_col__isnull=True)
                    | Q(
                        grid_row__isnull=False,
                        grid_col__isnull=False,
                        grid_row__gte=1,
                        grid_row__lte=30,
                        grid_col__gte=1,
                        grid_col__lte=30,
                    )
                ),
                name="ctrl_computer_grid_pair_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="computer",
            constraint=models.UniqueConstraint(
                condition=Q(
                    location__isnull=False,
                    grid_row__isnull=False,
                    grid_col__isnull=False,
                ),
                fields=("location", "grid_row", "grid_col"),
                name="ctrl_computer_location_grid_unique",
            ),
        ),
    ]

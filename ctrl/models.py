import uuid

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import (
    BooleanField,
    Case,
    Count,
    OuterRef,
    Q,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Now
from django.db.models.signals import post_delete, post_save, pre_delete
from django.dispatch import receiver
from django.contrib.auth.models import User
from constance import config


class Location(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    name = models.TextField()
    sequence_num = models.IntegerField(default=0, null=False)
    grid_cols = models.PositiveIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(30)],
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(grid_cols__isnull=True)
                | models.Q(grid_cols__gte=1, grid_cols__lte=30),
                name="ctrl_location_grid_cols_gt_zero",
            ),
        ]

    def clean(self):
        super().clean()
        if self.pk is None:
            return
        maximum_column = self.computer_set.aggregate(
            maximum=models.Max("grid_col")
        )["maximum"]
        if maximum_column is not None and (
            self.grid_cols is None or self.grid_cols < maximum_column
        ):
            raise ValidationError(
                {
                    "grid_cols": (
                        f"The class has a computer in column {maximum_column}; "
                        "move it before reducing the grid."
                    )
                }
            )

    def __str__(self):
        return f"{self.name}"


class ComputerQuerySet(models.QuerySet):
    def with_last_checkin(self):
        last_checkin = (
            CheckIn.objects
            .filter(computer=OuterRef("pk"))
            .order_by("-timestamp")
        )
        return self.annotate(
            last_checkin_timestamp=Subquery(
                last_checkin.values("timestamp")[:1]
            ),
            last_checkin_uptime=Subquery(
                last_checkin.values("uptime")[:1]
            ),
            rooted=Subquery(
                last_checkin.values("has_root")[:1]
            ),
        )

    def with_online_status(self):
        threshold = Now() - config.MACHINE_OFFLINE_THRESHOLD
        return (
            self.with_last_checkin()
            .annotate(
                is_online=Case(
                    When(
                        last_checkin_timestamp__gte=threshold,
                        then=Value(True),
                    ),
                    default=Value(False),
                    output_field=BooleanField(),
                )
            )
        )


class Computer(models.Model):
    machine_id = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=32)
    location = models.ForeignKey(Location, null=True, blank=True, on_delete=models.SET_NULL)
    sequence_num = models.IntegerField(default=0, null=False)
    grid_row = models.PositiveIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(30)],
    )
    grid_col = models.PositiveIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(30)],
    )

    objects = ComputerQuerySet.as_manager()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(grid_row__isnull=True, grid_col__isnull=True)
                    | models.Q(
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
            models.UniqueConstraint(
                fields=("location", "grid_row", "grid_col"),
                condition=models.Q(
                    location__isnull=False,
                    grid_row__isnull=False,
                    grid_col__isnull=False,
                ),
                name="ctrl_computer_location_grid_unique",
            ),
        ]

    def _clear_grid_after_location_change(self):
        if self.pk is None:
            return False
        previous_location_id = (
            type(self).objects.filter(pk=self.pk)
            .values_list("location_id", flat=True)
            .first()
        )
        if previous_location_id == self.location_id:
            return False
        self.grid_row = None
        self.grid_col = None
        return True

    def clean(self):
        self._clear_grid_after_location_change()
        super().clean()
        if (
            self.location_id is not None
            and self.grid_col is not None
            and self.location.grid_cols is not None
            and self.grid_col > self.location.grid_cols
        ):
            raise ValidationError(
                {
                    "grid_col": (
                        f"Column {self.grid_col} is outside the configured "
                        f"{self.location.grid_cols}-column class grid."
                    )
                }
            )

    def save(self, *args, **kwargs):
        if self._clear_grid_after_location_change():
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = set(update_fields) | {
                    "grid_row",
                    "grid_col",
                }
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.machine_id})"

    @property
    def contestant(self):
        assignment = getattr(self, "contestant_assignment", None)
        return assignment.contestant if assignment is not None else None


@receiver(pre_delete, sender=Location)
def clear_deleted_location_grid_positions(sender, instance, using, **kwargs):
    """An unassigned computer must not retain coordinates from a deleted room."""

    Computer.objects.using(using).filter(location_id=instance.pk).update(
        grid_row=None,
        grid_col=None,
    )


class CheckIn(models.Model):
    computer = models.ForeignKey(Computer, on_delete=models.CASCADE)
    timestamp = models.DateTimeField(auto_now_add=True)
    pseudo_timestamp = models.BigIntegerField(default=0)
    uptime = models.CharField(max_length=100, default="")
    has_root = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.computer.name} @ {self.timestamp}"


class UnknownComputer(models.Model):
    machine_id = models.CharField(max_length=40)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.machine_id}"


class TaskQuerySet(models.QuerySet):
    def with_ticket_status_counts(self):
        return self.annotate(
            new_count=Count("ticket", filter=Q(ticket__fetched__isnull=True)),
            in_progress_count=Count("ticket", filter=Q(ticket__fetched__isnull=False, ticket__completed__isnull=True)),
            completed_count=Count("ticket", filter=Q(ticket__completed__isnull=False)),
            error_count=Count("ticket", filter=Q(~Q(ticket__exit_code=0), ticket__completed__isnull=False)),
        )


class Task(models.Model):
    name = models.TextField()
    author = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    added = models.DateTimeField(auto_now_add=True)
    run_as = models.CharField(max_length=16)
    payload = models.TextField()

    objects = TaskQuerySet.as_manager()

    def __str__(self):
        return f"{self.name}"


class Ticket(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE)
    computer = models.ForeignKey(Computer, on_delete=models.CASCADE)
    added = models.DateTimeField(auto_now_add=True)
    fetched = models.DateTimeField(null=True, blank=True)
    completed = models.DateTimeField(null=True, blank=True)
    runtime = models.FloatField(null=True, blank=True)
    exit_code = models.IntegerField(null=True, blank=True)
    stdout = models.TextField(blank=True, default="")
    stderr = models.TextField(blank=True, default="")

    @property
    def is_new(self):
        return self.fetched is None

    @property
    def is_completed(self):
        return self.completed != None

    @property
    def is_in_progress(self):
        return not (self.is_new or self.is_completed)

    @property
    def status_string(self):
        if self.is_new:
            return 'new'
        elif self.is_completed:
            return 'done'
        elif self.is_in_progress:
            return 'in progress'
        else:
            return 'unknown'

    @property
    def runtime_rounded(self):
        return f'{self.runtime:.3f}s'

    def __str__(self):
        return f"{self.task.pk} @ {self.computer.name}"


class Student(models.Model):
    """A contestant identified only by the shared CMS/Olimp-control userid."""

    userid = models.CharField(max_length=64, unique=True, db_index=True)

    class Meta:
        ordering = ["userid"]
        verbose_name = "contestant"
        verbose_name_plural = "contestants"

    @property
    def computer(self):
        assignment = getattr(self, "computer_assignment", None)
        return assignment.computer if assignment is not None else None

    @property
    def locations(self):
        """The current computer's location, returned as a list for templates."""
        computer = self.computer
        return [computer.location] if computer and computer.location else []

    @property
    def assignment_anomalies(self):
        """Anomaly codes for the current one-to-one assignment."""
        computer = self.computer
        if computer is None:
            return ["no_computers"]
        if computer.location_id is None:
            return ["computer_without_class"]
        return []

    def __str__(self):
        return self.userid


class ContestantComputerAssignment(models.Model):
    """The single current contestant-to-computer mapping.

    Both sides are one-to-one at the database level. Rows are immutable:
    callers must delete the current mapping before creating a different one.
    """

    contestant = models.OneToOneField(
        Student,
        on_delete=models.PROTECT,
        related_name="computer_assignment",
    )
    computer = models.OneToOneField(
        Computer,
        on_delete=models.PROTECT,
        related_name="contestant_assignment",
    )
    assigned_at = models.DateTimeField(auto_now_add=True)
    source = models.CharField(max_length=64, default="manual", editable=False)

    class Meta:
        ordering = ["contestant__userid", "computer__machine_id"]
        verbose_name = "contestant-computer mapping"
        verbose_name_plural = "contestant-computer mappings"

    def _validate_immutable(self):
        if self.pk is None:
            return
        original = type(self).objects.filter(pk=self.pk).values(
            "contestant_id", "computer_id"
        ).first()
        if original is not None and (
            original["contestant_id"] != self.contestant_id
            or original["computer_id"] != self.computer_id
        ):
            raise ValidationError(
                "A mapping cannot be reassigned. Remove the existing mapping "
                "before creating a new one."
            )

    def clean(self):
        super().clean()
        self._validate_immutable()

    def save(self, *args, **kwargs):
        self._validate_immutable()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.contestant.userid} \u2194 {self.computer.machine_id}"


class ContestantComputerAssignmentEvent(models.Model):
    class Action(models.TextChoices):
        ASSIGNED = "assigned", "Assigned"
        REMOVED = "removed", "Removed"

    action = models.CharField(max_length=32, choices=Action.choices)
    assignment_id_snapshot = models.BigIntegerField()
    contestant_id_snapshot = models.BigIntegerField()
    computer_id_snapshot = models.BigIntegerField()
    contestant_identifier = models.CharField(max_length=64)
    computer_identifier = models.CharField(max_length=40)
    occurred_at = models.DateTimeField(auto_now_add=True)
    source = models.CharField(max_length=64, default="manual")
    actor_identifier = models.CharField(max_length=150, blank=True, default="")
    message = models.TextField()

    class Meta:
        ordering = ["occurred_at", "pk"]
        verbose_name = "contestant-computer mapping history event"
        verbose_name_plural = "contestant-computer mapping history"

    def __str__(self):
        return self.message


def _assignment_event(instance, action):
    verb = {
        ContestantComputerAssignmentEvent.Action.ASSIGNED: "Assigned",
        ContestantComputerAssignmentEvent.Action.REMOVED: "Removed assignment of",
    }[action]
    source = getattr(instance, "_event_source", None) or instance.source
    actor_identifier = getattr(instance, "_event_actor_identifier", "")
    ContestantComputerAssignmentEvent.objects.create(
        action=action,
        assignment_id_snapshot=instance.pk,
        contestant_id_snapshot=instance.contestant_id,
        computer_id_snapshot=instance.computer_id,
        contestant_identifier=instance.contestant.userid,
        computer_identifier=instance.computer.machine_id,
        source=source,
        actor_identifier=actor_identifier,
        message=(
            f'{verb} contestant "{instance.contestant.userid}" '
            f'{"to" if action == ContestantComputerAssignmentEvent.Action.ASSIGNED else "from"} '
            f'computer "{instance.computer.machine_id}".'
        ),
    )


@receiver(post_save, sender=ContestantComputerAssignment)
def record_assignment_created(sender, instance, created, raw, **kwargs):
    if created and not raw:
        _assignment_event(
            instance,
            ContestantComputerAssignmentEvent.Action.ASSIGNED,
        )


@receiver(post_delete, sender=ContestantComputerAssignment)
def record_assignment_removed(sender, instance, **kwargs):
    _assignment_event(
        instance,
        ContestantComputerAssignmentEvent.Action.REMOVED,
    )


class TaskPreset(models.Model):
    name = models.TextField()
    payload = models.TextField()

    def __str__(self):
        return self.name

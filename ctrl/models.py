from django.db import models
from django.db.models import Count, Q, When, Case, Value, BooleanField, Subquery, OuterRef
from django.db.models.functions import Now
from django.contrib.auth.models import User
from constance import config


class Location(models.Model):
    name = models.TextField()
    sequence_num = models.IntegerField(default=0, null=False)
    grid_cols = models.PositiveIntegerField(null=True, blank=True)

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
    machine_id = models.CharField(max_length=40, db_index=True)
    name = models.CharField(max_length=32)
    location = models.ForeignKey(Location, null=True, blank=True, on_delete=models.SET_NULL)
    sequence_num = models.IntegerField(default=0, null=False)
    grid_row = models.PositiveIntegerField(null=True, blank=True)
    grid_col = models.PositiveIntegerField(null=True, blank=True)

    objects = ComputerQuerySet.as_manager()

    def __str__(self):
        return f"{self.name} ({self.machine_id})"


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

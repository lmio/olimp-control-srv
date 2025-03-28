from django.db import models
from django.contrib.auth.models import User


class Computer(models.Model):
    machine_id = models.CharField(max_length=40)
    name = models.CharField(max_length=32)

    @property
    def most_recent_checkin(self):
        cs = self.checkin_set.order_by("-timestamp")[:1]
        return cs[0] if cs else None

    @property
    def rooted(self):
        c = self.most_recent_checkin
        return c.has_root if c else False

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


class Task(models.Model):
    name = models.TextField()
    author = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    added = models.DateTimeField(auto_now_add=True)
    run_as = models.CharField(max_length=16)
    payload = models.TextField()

    def __str__(self):
        return f"{self.name}"


class Ticket(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE)
    computer = models.ForeignKey(Computer, on_delete=models.CASCADE)
    added = models.DateTimeField(auto_now_add=True)
    fetched = models.DateTimeField(null=True)
    completed = models.DateTimeField(null=True)
    runtime = models.FloatField(null=True)
    exit_code = models.IntegerField(null=True)
    stdout = models.TextField(null=True)
    stderr = models.TextField(null=True)

    @property
    def is_new(self):
        return self.fetched is None

    @property
    def is_completed(self):
        return self.completed != None

    @property
    def is_in_progress(self):
        return not (self.is_new or self.is_completed)

    def __str__(self):
        return f"{self.task.pk} @ {self.computer.name}"

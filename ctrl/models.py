from django.db import models


class Computer(models.Model):
    machine_id = models.CharField(max_length=40)
    name = models.CharField(max_length=32)

    def __str__(self):
        return f"{self.name} ({self.machine_id})"


class CheckIn(models.Model):
    computer = models.ForeignKey(Computer, on_delete=models.CASCADE)
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.computer.name} @ {self.timestamp}"

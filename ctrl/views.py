from django.shortcuts import get_object_or_404, render
from django.http import HttpResponse

from .models import CheckIn, Computer


def index(request):
    computers = Computer.objects.order_by("name")
    context = {
        "computers": computers,
    }
    return HttpResponse(render(request, "ctrl/index.html", context))


def computer(request, machine_id):
    computer = get_object_or_404(Computer, machine_id=machine_id)
    checkins = computer.checkin_set.order_by("-timestamp")[:10]

    context = {
        "computer": computer,
        "checkins": checkins,
    }
    return HttpResponse(render(request, "ctrl/computer.html", context))

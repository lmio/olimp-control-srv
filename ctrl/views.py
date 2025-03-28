from django.shortcuts import render
from django.http import HttpResponse, Http404

from .models import Computer


def index(request):
    computers = Computer.objects.order_by("name")
    context = {
        "computers": computers,
    }
    return HttpResponse(render(request, "ctrl/index.html", context))


def computer(request, machine_id):
    try:
        computer = Computer.objects.get(machine_id=machine_id)
    except Computer.DoesNotExist:
        raise Http404("Computer does not exist")

    context = {
        "computer": computer,
    }
    return HttpResponse(render(request, "ctrl/computer.html", context))
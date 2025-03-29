from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, render
from django.http import HttpResponse
from django.views import generic

from .models import Computer, Task, Ticket


@login_required
def index(request):
    computers = Computer.objects.order_by("name")
    context = {
        "computers": computers,
    }
    return HttpResponse(render(request, "ctrl/index.html", context))


@login_required
def computer(request, machine_id):
    computer = get_object_or_404(Computer, machine_id=machine_id)
    checkins = computer.checkin_set.order_by("-timestamp")[:10]
    tickets = computer.ticket_set.order_by("-added")

    context = {
        "computer": computer,
        "checkins": checkins,
        "tickets": tickets,
    }
    return HttpResponse(render(request, "ctrl/computer.html", context))


class TaskView(LoginRequiredMixin, generic.DetailView):
    template_name = "ctrl/task.html"
    model = Task


class TaskListView(LoginRequiredMixin, generic.ListView):
    template_name = "ctrl/task_list.html"
    context_object_name = "task_list"

    def get_queryset(self):
        return Task.objects.order_by("-added")


class TicketView(LoginRequiredMixin, generic.DetailView):
    template_name = "ctrl/ticket.html"
    model = Ticket

import json

from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Prefetch, Q, Exists, OuterRef
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views import generic

from .models import Location, Computer, UnknownComputer, Task, Ticket, TaskPreset
from .forms import NewTaskForm, RegisterComputerForm


def _computer_queryset():
    return (
        Computer.objects
        .with_online_status()
        .annotate(
            has_in_progress=Exists(
                Ticket.objects.filter(
                    computer=OuterRef("pk"),
                    fetched__isnull=False,
                    completed__isnull=True,
                )
            )
        )
        .order_by("sequence_num")
    )


def _get_status_context():
    ordered_computer_queryset = _computer_queryset()
    locations = list(Location.objects.order_by("sequence_num").prefetch_related(Prefetch(
        "computer_set",
        queryset=ordered_computer_queryset
    )))
    unassigned_computers = list(ordered_computer_queryset.filter(location=None))

    all_computers = [c for loc in locations for c in loc.computer_set.all()] + unassigned_computers
    online_count = sum(1 for c in all_computers if c.is_online)
    total_count = len(all_computers)

    unknown_count = UnknownComputer.objects.count()

    return {
        "locations": locations,
        "unassigned_computers": unassigned_computers,
        "online_count": online_count,
        "offline_count": total_count - online_count,
        "total_count": total_count,
        "unknown_count": unknown_count,
    }


@login_required
def index(request):
    return render(request, "ctrl/index.html", _get_status_context())


@login_required
def index_status_partial(request):
    return render(request, "ctrl/partial/index_status_partial.html", _get_status_context())


def _split_placed(computers):
    """Split computers into (placed, unplaced) based on grid_row/grid_col."""
    placed = [c for c in computers if c.grid_row is not None and c.grid_col is not None]
    unplaced = [c for c in computers if c.grid_row is None or c.grid_col is None]
    return placed, unplaced


def _get_location_context(pk):
    location = get_object_or_404(Location, pk=pk)
    computers = list(_computer_queryset().filter(location=location))
    placed, unplaced = _split_placed(computers)
    online_count = sum(1 for c in computers if c.is_online)

    return {
        "location": location,
        "computers": computers,
        "placed_computers": placed,
        "unplaced_computers": unplaced,
        "online_count": online_count,
        "offline_count": len(computers) - online_count,
        "total_count": len(computers),
    }


@login_required
def location_detail(request, pk):
    return render(request, "ctrl/location.html", _get_location_context(pk))


@login_required
def location_detail_partial(request, pk):
    return render(request, "ctrl/partial/location_partial.html", _get_location_context(pk))


@login_required
def location_edit_layout(request, pk):
    location = get_object_or_404(Location, pk=pk)
    computers = list(Computer.objects.filter(location=location).order_by("sequence_num"))
    placed, unplaced = _split_placed(computers)
    grid_rows = max((c.grid_row for c in placed), default=4)

    return render(request, "ctrl/location_edit_layout.html", {
        "location": location,
        "placed_computers": placed,
        "unplaced_computers": unplaced,
        "grid_rows": grid_rows,
    })


@login_required
def location_save_layout(request, pk):
    if request.method != "POST":
        return redirect("ctrl.location_edit_layout", pk=pk)

    location = get_object_or_404(Location, pk=pk)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    grid_cols = data.get("grid_cols")
    positions = data.get("positions", [])

    occupied = set()
    for p in positions:
        if p.get("row") is not None and p.get("col") is not None:
            key = (p["row"], p["col"])
            if key in occupied:
                return JsonResponse({"error": f"Duplicate position {key}"}, status=400)
            occupied.add(key)

    computer_ids = [p["id"] for p in positions]
    computers = {c.pk: c for c in Computer.objects.filter(pk__in=computer_ids, location=location)}

    with transaction.atomic():
        if grid_cols is not None:
            location.grid_cols = grid_cols
            location.save(update_fields=["grid_cols"])

        for p in positions:
            comp = computers.get(p["id"])
            if comp:
                comp.grid_row = p.get("row")
                comp.grid_col = p.get("col")
                comp.save(update_fields=["grid_row", "grid_col"])

    return JsonResponse({"ok": True})


@login_required
def computer(request, machine_id):
    computer = get_object_or_404(Computer.objects.with_last_checkin(), machine_id=machine_id)
    checkins = computer.checkin_set.order_by("-timestamp")[:10]
    tickets = computer.ticket_set.select_related("task").order_by("-added")

    context = {
        "computer": computer,
        "checkins": checkins,
        "tickets": tickets,
    }
    return render(request, "ctrl/computer.html", context)


def _get_task_context(pk):
    task = get_object_or_404(Task.objects.with_ticket_status_counts(), pk=pk)
    tickets = task.ticket_set.select_related("computer", "computer__location").order_by("-pk")
    return {"task": task, "tickets": tickets}


@login_required
def task(request, pk):
    return render(request, "ctrl/task.html", _get_task_context(pk))


@login_required
def task_partial(request, pk):
    return render(request, "ctrl/partial/task_tickets_partial.html", _get_task_context(pk))


def _render_create_task_html(request, form):
    computers_by_location = {}
    for c in form.fields["computers"].queryset.select_related("location"):
        loc_name = c.location.name if c.location else "Unassigned"
        computers_by_location.setdefault(loc_name, []).append(c)

    task_presets = TaskPreset.objects.all()
    return render(request, "ctrl/create_task.html", {
        "form": form,
        "computers_by_location": computers_by_location,
        "task_presets": task_presets,
    })


@login_required
def create_task(request):
    if request.method == "POST":
        form = NewTaskForm(request.POST)
        if form.is_valid():
            task = form.save(commit=False)
            task.author = request.user
            task_computers = form.cleaned_data["computers"]
            tickets = [Ticket(task=task, computer=computer)
                       for computer in task_computers]
            with transaction.atomic():
                task.save()
                Ticket.objects.bulk_create(tickets)
            return redirect("ctrl.task", pk=task.pk)
    else:
        clone_pk = request.GET.get("clone")
        if clone_pk:
            old_task = get_object_or_404(Task, pk=clone_pk)
            old_computers = [t.computer for t in old_task.ticket_set.select_related("computer").all()]
            form = NewTaskForm(initial={
                "name": f"{old_task.name} (kopija)",
                "run_as": old_task.run_as,
                "payload": old_task.payload,
                "computers": old_computers,
            })
        else:
            form = NewTaskForm()

    return _render_create_task_html(request, form)

@login_required
def unknown_computers(request):
    allowed_sort = {
        "mid": "machine_id",
        "first": "first_seen",
        "last": "last_seen",
        "-mid": "-machine_id",
        "-first": "-first_seen",
        "-last": "-last_seen",
    }
    sort = request.GET.get("sort", "-last")
    order_by = allowed_sort.get(sort, "-last_seen")
    computers = UnknownComputer.objects.order_by(order_by)[:100]
    return render(request, "ctrl/unknown_computers.html", {
        "computers": computers,
        "sort": sort,
    })


@login_required
def register_computer(request, pk):
    uc = get_object_or_404(UnknownComputer, pk=pk)

    if Computer.objects.filter(machine_id=uc.machine_id).exists():
        uc.delete()
        return redirect("ctrl.unknown_computers")

    if request.method == "POST":
        form = RegisterComputerForm(request.POST)
        if form.is_valid():
            Computer.objects.create(
                machine_id=uc.machine_id,
                name=form.cleaned_data["name"],
                location=form.cleaned_data["location"],
            )
            uc.delete()
            return redirect("ctrl.unknown_computers")
    else:
        form = RegisterComputerForm()

    return render(request, "ctrl/register_computer.html", {
        "uc": uc,
        "form": form,
    })


class TaskListView(LoginRequiredMixin, generic.ListView):
    template_name = "ctrl/task_list.html"
    context_object_name = "task_list"

    def get_queryset(self):
        return Task.objects.with_ticket_status_counts().select_related("author").order_by("-added")


class TicketView(LoginRequiredMixin, generic.DetailView):
    template_name = "ctrl/ticket.html"
    model = Ticket

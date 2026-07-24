import json

from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib import messages
from django.db.models import Count, Prefetch, Q, Exists, OuterRef
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views import generic

from .models import (
    Location,
    Computer,
    ContestantComputerAssignment,
    Student,
    UnknownComputer,
    Task,
    Ticket,
    TaskPreset,
)
from .forms import (
    ComputerEditForm,
    ContestantComputerAssignmentForm,
    LocationForm,
    NewTaskForm,
    RegisterComputerForm,
    StudentForm,
)
from .assignments import (
    AssignmentConflict,
    assign_contestant_to_computer,
    remove_contestant_computer_assignment,
)


def _computer_queryset():
    return (
        Computer.objects
        .select_related("contestant_assignment__contestant")
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
    placed = [
        c
        for c in computers
        if isinstance(c.grid_row, int)
        and isinstance(c.grid_col, int)
        and c.grid_row > 0
        and c.grid_col > 0
    ]
    unplaced = [c for c in computers if c not in placed]
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

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({"error": "Layout payload must be an object"}, status=400)

    grid_cols = data.get("grid_cols")
    if type(grid_cols) is not int or not 1 <= grid_cols <= 30:
        return JsonResponse(
            {"error": "grid_cols must be an integer between 1 and 30"},
            status=400,
        )
    positions = data.get("positions")
    if not isinstance(positions, list):
        return JsonResponse({"error": "positions must be a list"}, status=400)

    with transaction.atomic():
        location = get_object_or_404(
            Location.objects.select_for_update(),
            pk=pk,
        )
        computers = list(
            Computer.objects.select_for_update()
            .filter(location=location)
            .order_by("pk")
        )
        expected_ids = {computer.pk for computer in computers}
        normalized_positions = {}
        occupied = set()
        for index, position in enumerate(positions):
            if not isinstance(position, dict):
                return JsonResponse(
                    {"error": f"positions[{index}] must be an object"},
                    status=400,
                )
            computer_id = position.get("id")
            if type(computer_id) is not int or computer_id <= 0:
                return JsonResponse(
                    {"error": f"positions[{index}].id must be a positive integer"},
                    status=400,
                )
            if computer_id in normalized_positions:
                return JsonResponse(
                    {"error": f"Duplicate computer id {computer_id}"},
                    status=400,
                )
            row = position.get("row")
            col = position.get("col")
            if (row is None) != (col is None):
                return JsonResponse(
                    {"error": "row and col must either both be set or both be null"},
                    status=400,
                )
            if row is not None:
                if (
                    type(row) is not int
                    or type(col) is not int
                    or not 1 <= row <= 30
                    or not 1 <= col <= grid_cols
                ):
                    return JsonResponse(
                        {
                            "error": (
                                "placed rows must be between 1 and 30 and "
                                "columns must fit grid_cols"
                            )
                        },
                        status=400,
                    )
                coordinate = (row, col)
                if coordinate in occupied:
                    return JsonResponse(
                        {"error": f"Duplicate position {coordinate}"},
                        status=400,
                    )
                occupied.add(coordinate)
            normalized_positions[computer_id] = (row, col)

        submitted_ids = set(normalized_positions)
        if submitted_ids != expected_ids:
            return JsonResponse(
                {
                    "error": "positions must contain every computer in this class exactly once",
                    "missing_ids": sorted(expected_ids - submitted_ids),
                    "unknown_ids": sorted(submitted_ids - expected_ids),
                },
                status=400,
            )

        location.grid_cols = grid_cols
        location.save(update_fields=["grid_cols"])
        # The database enforces unique occupied cells immediately. Clear the
        # locked class rows first so a valid swap cannot collide with the old
        # coordinates while the final state is being written.
        Computer.objects.filter(pk__in=expected_ids).update(
            grid_row=None,
            grid_col=None,
        )
        for computer in computers:
            computer.grid_row, computer.grid_col = normalized_positions[computer.pk]
        Computer.objects.bulk_update(computers, ["grid_row", "grid_col"])

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


@login_required
def computer_edit(request, machine_id):
    computer = get_object_or_404(Computer, machine_id=machine_id)
    original_location_id = computer.location_id
    if request.method == "POST":
        form = ComputerEditForm(request.POST, instance=computer)
        if form.is_valid():
            with transaction.atomic():
                updated = form.save(commit=False)
                if updated.location_id != original_location_id:
                    updated.grid_row = None
                    updated.grid_col = None
                updated.save()
            return redirect("ctrl.computer", machine_id=computer.machine_id)
    else:
        form = ComputerEditForm(instance=computer)
    return render(
        request,
        "ctrl/computer_edit.html",
        {"computer": computer, "form": form},
    )


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


@login_required
def student_list(request):
    students = list(
        Student.objects.select_related(
            "computer_assignment__computer__location"
        )
    )
    for student in students:
        student.anomaly_codes = student.assignment_anomalies
    return render(request, "ctrl/student_list.html", {"students": students})


@login_required
def student_edit(request, pk=None):
    student = get_object_or_404(Student, pk=pk) if pk is not None else None

    if request.method == "POST":
        form = StudentForm(request.POST, instance=student)
        if form.is_valid():
            form.save()
            return redirect("ctrl.student_list")
    else:
        form = StudentForm(instance=student)

    return render(request, "ctrl/student_edit.html", {
        "form": form,
        "student": student,
    })


@login_required
def student_delete(request, pk):
    student = get_object_or_404(Student, pk=pk)
    if request.method == "POST":
        if student.computer is not None:
            messages.error(
                request,
                "Pirmiausia pašalinkite mokinio kompiuterio priskyrimą.",
            )
            return redirect("ctrl.student_list")
        student.delete()
    return redirect("ctrl.student_list")


@login_required
def assignment_list(request):
    assignments = (
        ContestantComputerAssignment.objects.select_related(
            "contestant", "computer", "computer__location"
        )
        .order_by("contestant__userid")
    )
    return render(
        request,
        "ctrl/assignment_list.html",
        {"assignments": assignments},
    )


@login_required
def assignment_new(request):
    if request.method == "POST":
        form = ContestantComputerAssignmentForm(request.POST)
        if form.is_valid():
            try:
                assign_contestant_to_computer(
                    contestant=form.cleaned_data["contestant"],
                    computer=form.cleaned_data["computer"],
                    source="management_ui",
                    actor_identifier=request.user.get_username(),
                )
            except AssignmentConflict as error:
                form.add_error(None, str(error))
            else:
                return redirect("ctrl.assignment_list")
    else:
        form = ContestantComputerAssignmentForm()
    return render(request, "ctrl/assignment_edit.html", {"form": form})


@login_required
def assignment_delete(request, pk):
    assignment = get_object_or_404(
        ContestantComputerAssignment.objects.select_related(
            "contestant", "computer"
        ),
        pk=pk,
    )
    if request.method == "POST":
        remove_contestant_computer_assignment(
            assignment=assignment,
            source="management_ui",
            actor_identifier=request.user.get_username(),
        )
    return redirect("ctrl.assignment_list")


@login_required
def location_list(request):
    locations = Location.objects.order_by("sequence_num").annotate(
        computer_count=Count("computer", distinct=True),
        student_count=Count(
            "computer__contestant_assignment__contestant",
            distinct=True,
        ),
    )
    return render(request, "ctrl/location_list.html", {"locations": locations})


@login_required
def location_edit(request, pk=None):
    location = get_object_or_404(Location, pk=pk) if pk is not None else None

    if request.method == "POST":
        form = LocationForm(request.POST, instance=location)
        if form.is_valid():
            form.save()
            return redirect("ctrl.location_list")
    else:
        form = LocationForm(instance=location)

    return render(request, "ctrl/location_edit.html", {
        "form": form,
        "location": location,
    })


@login_required
def location_delete(request, pk):
    location = get_object_or_404(Location, pk=pk)
    if request.method == "POST":
        location.delete()
    return redirect("ctrl.location_list")


class TaskListView(LoginRequiredMixin, generic.ListView):
    template_name = "ctrl/task_list.html"
    context_object_name = "task_list"

    def get_queryset(self):
        return Task.objects.with_ticket_status_counts().select_related("author").order_by("-added")


class TicketView(LoginRequiredMixin, generic.DetailView):
    template_name = "ctrl/ticket.html"
    model = Ticket

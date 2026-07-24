"""Transactional operations for current contestant-computer mappings."""

from django.db import IntegrityError, transaction
from django.db.models import Q

from .models import Computer, ContestantComputerAssignment, Student


class AssignmentConflict(Exception):
    """Raised when either side already belongs to another current mapping."""


def _conflict_message(*, contestant, computer, existing):
    if existing.contestant_id == contestant.pk:
        return (
            f'Contestant "{contestant.userid}" is already assigned to computer '
            f'"{existing.computer.machine_id}". Remove that mapping before '
            "assigning a new one."
        )
    return (
        f'Computer "{computer.machine_id}" is already assigned to contestant '
        f'"{existing.contestant.userid}". Remove that mapping before assigning '
        "a new one."
    )


@transaction.atomic
def assign_contestant_to_computer(
    *,
    contestant,
    computer,
    source,
    actor_identifier="",
):
    """Create one current mapping without ever replacing another mapping.

    Repeating the same pair is idempotent. Both endpoint rows are locked before
    the mapping lookup so normal API/admin callers serialize cleanly; database
    uniqueness remains the final guard for concurrent inserts.
    """

    contestant = Student.objects.select_for_update().get(pk=contestant.pk)
    computer = Computer.objects.select_for_update().get(pk=computer.pk)
    existing_rows = list(
        ContestantComputerAssignment.objects.select_for_update()
        .select_related("contestant", "computer")
        .filter(
            Q(contestant=contestant)
            | Q(computer=computer)
        )
        .order_by("pk")
    )
    for existing in existing_rows:
        if (
            existing.contestant_id == contestant.pk
            and existing.computer_id == computer.pk
        ):
            return existing, False
        raise AssignmentConflict(
            _conflict_message(
                contestant=contestant,
                computer=computer,
                existing=existing,
            )
        )

    try:
        with transaction.atomic():
            assignment = ContestantComputerAssignment(
                contestant=contestant,
                computer=computer,
                source=source,
            )
            assignment._event_actor_identifier = actor_identifier
            assignment.save()
    except IntegrityError as error:
        existing = (
            ContestantComputerAssignment.objects.select_related(
                "contestant", "computer"
            )
            .filter(
                Q(contestant=contestant)
                | Q(computer=computer)
            )
            .order_by("pk")
            .first()
        )
        if existing is not None:
            if (
                existing.contestant_id == contestant.pk
                and existing.computer_id == computer.pk
            ):
                return existing, False
            raise AssignmentConflict(
                _conflict_message(
                    contestant=contestant,
                    computer=computer,
                    existing=existing,
                )
            ) from error
        raise
    return assignment, True


@transaction.atomic
def remove_contestant_computer_assignment(
    *,
    assignment,
    source,
    actor_identifier="",
):
    """Explicitly remove one current mapping and record the removal source."""

    current = (
        ContestantComputerAssignment.objects.select_for_update()
        .select_related("contestant", "computer")
        .filter(pk=assignment.pk)
        .first()
    )
    if current is None:
        return False
    current._event_source = source
    current._event_actor_identifier = actor_identifier
    current.delete()
    return True

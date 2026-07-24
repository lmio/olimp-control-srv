from django.contrib import admin, messages
from django.http import HttpResponseRedirect
from django import forms
from django.db.models import Q
from import_export.admin import ImportMixin
from import_export.formats import base_formats

from .assignments import (
    AssignmentConflict,
    assign_contestant_to_computer,
    remove_contestant_computer_assignment,
)
from . import models
from .resources import (
    ComputerResource,
    ContestantComputerAssignmentResource,
    ContestantResource,
)


class CheckInAdmin(admin.ModelAdmin):
    readonly_fields = ["timestamp"]


class UnknownComputerAdmin(admin.ModelAdmin):
    readonly_fields = ["first_seen", "last_seen"]


@admin.register(models.Computer)
class ComputerAdmin(ImportMixin, admin.ModelAdmin):
    resource_classes = [ComputerResource]
    import_formats = [base_formats.CSV]
    list_display = [
        "name",
        "machine_id",
        "location",
        "contestant_identifier",
        "sequence_num",
    ]
    list_filter = ["location"]
    search_fields = [
        "name",
        "machine_id",
        "contestant_assignment__contestant__userid",
    ]

    def has_import_permission(self, request):
        return request.user.has_perms(
            ("ctrl.add_computer", "ctrl.change_computer")
        )

    @admin.display(description="Contestant")
    def contestant_identifier(self, computer):
        contestant = computer.contestant
        return contestant.userid if contestant is not None else None

    def get_import_data_kwargs(self, **kwargs):
        values = super().get_import_data_kwargs(**kwargs)
        values["rollback_on_validation_errors"] = True
        return values


@admin.register(models.Student)
class StudentAdmin(ImportMixin, admin.ModelAdmin):
    resource_classes = [ContestantResource]
    import_formats = [base_formats.CSV]
    list_display = ["userid", "computer_identifier"]
    search_fields = ["userid"]

    @admin.display(description="Computer")
    def computer_identifier(self, student):
        computer = student.computer
        return computer.machine_id if computer is not None else None

    def get_import_data_kwargs(self, **kwargs):
        values = super().get_import_data_kwargs(**kwargs)
        values["rollback_on_validation_errors"] = True
        return values


class ContestantComputerAssignmentAdminForm(forms.ModelForm):
    class Meta:
        model = models.ContestantComputerAssignment
        fields = ["contestant", "computer"]

    def clean(self):
        cleaned = super().clean()
        contestant = cleaned.get("contestant")
        computer = cleaned.get("computer")
        if contestant is None or computer is None:
            return cleaned
        existing = (
            models.ContestantComputerAssignment.objects.select_related(
                "contestant", "computer"
            )
            .filter(Q(contestant=contestant) | Q(computer=computer))
            .exclude(pk=self.instance.pk)
            .first()
        )
        if existing is None:
            return cleaned
        if existing.contestant_id == contestant.pk:
            self.add_error(
                "contestant",
                (
                    f'Contestant "{contestant.userid}" is already assigned to '
                    f'computer "{existing.computer.machine_id}". Remove that '
                    "mapping before assigning a new one."
                ),
            )
        if existing.computer_id == computer.pk:
            self.add_error(
                "computer",
                (
                    f'Computer "{computer.machine_id}" is already assigned to '
                    f'contestant "{existing.contestant.userid}". Remove that '
                    "mapping before assigning a new one."
                ),
            )
        return cleaned


@admin.register(models.ContestantComputerAssignment)
class ContestantComputerAssignmentAdmin(ImportMixin, admin.ModelAdmin):
    form = ContestantComputerAssignmentAdminForm
    resource_classes = [ContestantComputerAssignmentResource]
    import_formats = [base_formats.CSV]
    list_display = ["contestant", "computer", "assigned_at", "source"]
    list_select_related = ["contestant", "computer"]
    search_fields = ["contestant__userid", "computer__machine_id"]
    readonly_fields = ["assigned_at", "source"]

    def get_import_data_kwargs(self, **kwargs):
        values = super().get_import_data_kwargs(**kwargs)
        values["rollback_on_validation_errors"] = True
        return values

    def add_view(self, request, form_url="", extra_context=None):
        try:
            return super().add_view(request, form_url, extra_context)
        except AssignmentConflict as error:
            # A competing request can occupy an endpoint after form validation.
            # The service keeps the state valid; return a clear remove-first
            # instruction instead of exposing an IntegrityError/HTTP 500.
            self.message_user(request, str(error), level=messages.ERROR)
            return HttpResponseRedirect(request.path)

    def get_readonly_fields(self, request, obj=None):
        if obj is not None:
            return ("contestant", "computer", "assigned_at", "source")
        return super().get_readonly_fields(request, obj)

    def save_model(self, request, obj, form, change):
        if change:
            super().save_model(request, obj, form, change)
            return
        saved, _created = assign_contestant_to_computer(
            contestant=obj.contestant,
            computer=obj.computer,
            source="django_admin",
            actor_identifier=request.user.get_username(),
        )
        obj.pk = saved.pk
        obj.assigned_at = saved.assigned_at
        obj.source = saved.source
        obj._state.adding = False
        obj._state.db = saved._state.db

    def delete_model(self, request, obj):
        remove_contestant_computer_assignment(
            assignment=obj,
            source="django_admin",
            actor_identifier=request.user.get_username(),
        )

    def delete_queryset(self, request, queryset):
        for assignment in queryset.select_related("contestant", "computer"):
            remove_contestant_computer_assignment(
                assignment=assignment,
                source="django_admin",
                actor_identifier=request.user.get_username(),
            )


@admin.register(models.ContestantComputerAssignmentEvent)
class ContestantComputerAssignmentEventAdmin(admin.ModelAdmin):
    list_display = [
        "occurred_at",
        "action",
        "contestant_identifier",
        "computer_identifier",
        "source",
        "actor_identifier",
    ]
    list_filter = ["action", "source"]
    search_fields = [
        "contestant_identifier",
        "computer_identifier",
        "actor_identifier",
        "message",
    ]
    readonly_fields = [
        "action",
        "assignment_id_snapshot",
        "contestant_id_snapshot",
        "computer_id_snapshot",
        "contestant_identifier",
        "computer_identifier",
        "occurred_at",
        "source",
        "actor_identifier",
        "message",
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(models.Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ["name", "public_id", "sequence_num", "grid_cols"]

    def get_readonly_fields(self, request, obj=None):
        fields = ["public_id"]
        if request.user.has_perm("ctrl.change_computer"):
            return tuple(fields)
        fields.append("grid_cols")
        return tuple(fields)

    def has_delete_permission(self, request, obj=None):
        return (
            super().has_delete_permission(request, obj)
            and request.user.has_perm("ctrl.change_computer")
        )


admin.site.register(
    (
        models.Task,
        models.Ticket,
        models.TaskPreset,
    )
)
admin.site.register(models.CheckIn, CheckInAdmin)
admin.site.register(models.UnknownComputer, UnknownComputerAdmin)

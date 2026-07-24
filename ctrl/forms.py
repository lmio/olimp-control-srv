from django import forms
from django.db.models import Q
from django.forms.widgets import Textarea

from .models import (
    Computer,
    ContestantComputerAssignment,
    Location,
    Student,
    Task,
)


class ComputerMultipleChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        location_name = obj.location.name if obj.location else "Unknown location"
        return f"{location_name}: {obj.name}"


class NewTaskForm(forms.ModelForm):
    name = forms.CharField(label="Name", required=True)
    run_as = forms.CharField(label="Execute as", initial="root", required=True)
    computers = ComputerMultipleChoiceField(
        queryset=Computer.objects.order_by(
            "location__sequence_num",
            "location__name",
            "sequence_num",
            "name",
        ),
        widget=forms.SelectMultiple,
        required=True,
    )
    payload = forms.CharField(label="Payload", widget=Textarea, required=True)

    def clean_payload(self):
        data = self.cleaned_data.get("payload", "")
        return data.replace("\r\n", "\n")

    class Meta:
        model = Task
        fields = ["name", "run_as", "computers", "payload"]


class RegisterComputerForm(forms.Form):
    name = forms.CharField(max_length=32)
    location = forms.ModelChoiceField(
        queryset=Location.objects.order_by("sequence_num"),
        required=False,
    )


class ComputerEditForm(forms.ModelForm):
    class Meta:
        model = Computer
        fields = ["name", "location", "sequence_num"]
        labels = {
            "name": "Pavadinimas",
            "location": "Klasė",
            "sequence_num": "Eilės nr.",
        }


class StudentForm(forms.ModelForm):
    class Meta:
        model = Student
        fields = ["userid"]
        labels = {"userid": "Naudotojo ID"}


class ContestantComputerAssignmentForm(forms.Form):
    contestant = forms.ModelChoiceField(
        label="Mokinys",
        queryset=Student.objects.order_by("userid"),
    )
    computer = forms.ModelChoiceField(
        label="Kompiuteris",
        queryset=Computer.objects.order_by(
            "location__sequence_num",
            "location__name",
            "sequence_num",
            "name",
            "machine_id",
        ),
    )

    def clean(self):
        cleaned = super().clean()
        contestant = cleaned.get("contestant")
        computer = cleaned.get("computer")
        if contestant is None or computer is None:
            return cleaned
        existing = (
            ContestantComputerAssignment.objects.select_related(
                "contestant", "computer"
            )
            .filter(Q(contestant=contestant) | Q(computer=computer))
            .first()
        )
        if existing is None:
            return cleaned
        if existing.contestant_id == contestant.pk:
            self.add_error(
                "contestant",
                (
                    f'Mokinys „{contestant.userid}“ jau priskirtas '
                    f'kompiuteriui „{existing.computer.machine_id}“. '
                    "Pirmiausia pašalinkite esamą priskyrimą."
                ),
            )
        if existing.computer_id == computer.pk:
            self.add_error(
                "computer",
                (
                    f'Kompiuteris „{computer.machine_id}“ jau priskirtas '
                    f'mokiniui „{existing.contestant.userid}“. '
                    "Pirmiausia pašalinkite esamą priskyrimą."
                ),
            )
        return cleaned


class LocationForm(forms.ModelForm):
    class Meta:
        model = Location
        fields = ["name", "sequence_num", "grid_cols"]
        labels = {
            "name": "Pavadinimas",
            "sequence_num": "Eilės nr.",
            "grid_cols": "Tinklelio stulpeliai",
        }

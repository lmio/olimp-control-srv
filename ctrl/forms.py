from django import forms
from django.forms.widgets import Textarea
from .models import Computer, Location, Task


class ComputerMultipleChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        if obj.location:
            location_name = obj.location.name
        else:
            location_name = 'Unknown location'
        return f'{location_name}: {obj.name}'


class NewTaskForm(forms.ModelForm):
    name = forms.CharField(
        label="Name",
        required=True
    )
    run_as = forms.CharField(
        label="Execute as",
        initial="root",
        required=True
    )
    computers = ComputerMultipleChoiceField(
        queryset=Computer.objects.order_by(
            "location__sequence_num",
            "location__name",
            "sequence_num",
            "name"
        ),
        widget=forms.SelectMultiple,
        required=True,
    )
    payload = forms.CharField(
        label="Payload",
        widget=Textarea,
        required=True
    )

    def clean_payload(self):
        data = self.cleaned_data.get('payload', '')
        normalized_data = data.replace('\r\n', '\n')
        return normalized_data

    class Meta:
        model = Task
        fields = ['name', 'run_as', 'computers', 'payload']


class RegisterComputerForm(forms.Form):
    name = forms.CharField(max_length=32)
    location = forms.ModelChoiceField(
        queryset=Location.objects.order_by("sequence_num"),
        required=False,
    )


from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="ctrl.index"),
    path("computer/<str:machine_id>/", views.computer, name="ctrl.computer"),
]

from django.urls import path

from . import apiviews as views

urlpatterns = [
    path("ping", views.ping, name="ctrl.api.ping"),
]

from django.urls import path

from . import apiviews as views

urlpatterns = [
    path("ping", views.ping, name="ctrl.api.ping"),
    path("ticket", views.ticket, name="ctrl.api.ticket"),
]

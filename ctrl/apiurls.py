from django.urls import path

from . import apiviews as views
from . import management_api
from . import toilet_api

urlpatterns = [
    path("ping", views.ping, name="ctrl.api.ping"),
    path("ticket", views.ticket, name="ctrl.api.ticket"),
    path(
        "toilet/v1/classes",
        toilet_api.classes,
        name="ctrl.api.toilet.classes",
    ),
    path(
        "toilet/v1/classes/<uuid:public_id>/layout",
        toilet_api.class_layout,
        name="ctrl.api.toilet.class_layout",
    ),
    path(
        "toilet/v1/students",
        toilet_api.students,
        name="ctrl.api.toilet.students",
    ),
    path(
        "toilet/v1/student-assignment",
        toilet_api.student_assignment,
        name="ctrl.api.toilet.student_assignment",
    ),
    path(
        "management/v1/state",
        management_api.state,
        name="ctrl.api.management.state",
    ),
    path(
        "management/v1/classes/<str:public_id>",
        management_api.put_class,
        name="ctrl.api.management.class",
    ),
    path(
        "management/v1/computers/<str:machine_id>",
        management_api.put_computer,
        name="ctrl.api.management.computer",
    ),
    path(
        "management/v1/students/<str:userid>",
        management_api.put_student,
        name="ctrl.api.management.student",
    ),
    path(
        "management/v1/contestant-computer-mappings/<str:userid>",
        management_api.put_assignment,
        name="ctrl.api.management.assignment",
    ),
    path(
        "management/v1/admins/<str:username>",
        management_api.admin_account,
        name="ctrl.api.management.admin",
    ),
]

from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="ctrl.index"),
    path("status-partial/", views.index_status_partial, name="ctrl.index_status_partial"),
    path("location/<int:pk>/", views.location_detail, name="ctrl.location"),
    path("location/<int:pk>/partial/", views.location_detail_partial, name="ctrl.location_partial"),
    path("location/<int:pk>/edit-layout/", views.location_edit_layout, name="ctrl.location_edit_layout"),
    path("location/<int:pk>/save-layout/", views.location_save_layout, name="ctrl.location_save_layout"),
    path("computer/<str:machine_id>/", views.computer, name="ctrl.computer"),
    path("task_list/", views.TaskListView.as_view(), name="ctrl.task_list"),
    path("task/<int:pk>/", views.task, name="ctrl.task"),
    path("task/<int:pk>/partial/", views.task_partial, name="ctrl.task_partial"),
    path("create-task/", views.create_task, name="ctrl.create_task"),
    path("ticket/<int:pk>/", views.TicketView.as_view(), name="ctrl.ticket"),
    path("students/", views.student_list, name="ctrl.student_list"),
    path("students/new/", views.student_edit, name="ctrl.student_new"),
    path("students/<int:pk>/edit/", views.student_edit, name="ctrl.student_edit"),
    path("students/<int:pk>/delete/", views.student_delete, name="ctrl.student_delete"),
    path("classes/", views.location_list, name="ctrl.location_list"),
    path("classes/new/", views.location_edit, name="ctrl.location_new"),
    path("classes/<int:pk>/edit/", views.location_edit, name="ctrl.location_edit"),
    path("classes/<int:pk>/delete/", views.location_delete, name="ctrl.location_delete"),
    path("unknown/", views.unknown_computers, name="ctrl.unknown_computers"),
    path("unknown/<int:pk>/register/", views.register_computer, name="ctrl.register_computer"),
]

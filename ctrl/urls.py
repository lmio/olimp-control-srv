from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="ctrl.index"),
    path("computer/<str:machine_id>/", views.computer, name="ctrl.computer"),
    path("task_list/", views.TaskListView.as_view(), name="ctrl.task_list"),
    path("task/<int:pk>/", views.TaskView.as_view(), name="ctrl.task"),
    path("ticket/<int:pk>/", views.TicketView.as_view(), name="ctrl.ticket"),
]

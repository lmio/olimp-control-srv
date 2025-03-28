from django.contrib import admin
from . import models


class CheckInAdmin(admin.ModelAdmin):
    readonly_fields = ["timestamp"]


class UnknownComputerAdmin(admin.ModelAdmin):
    readonly_fields = ["first_seen", "last_seen"]


admin.site.register(
    (
        models.Computer,
    )
)
admin.site.register(models.CheckIn, CheckInAdmin)
admin.site.register(models.UnknownComputer, UnknownComputerAdmin)

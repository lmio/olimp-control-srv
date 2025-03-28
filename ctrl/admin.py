from django.contrib import admin
from . import models


class UnknownComputerAdmin(admin.ModelAdmin):
    readonly_fields = ["first_seen", "last_seen"]

admin.site.register(
    (
        models.Computer,
        models.CheckIn,
    )
)

admin.site.register(models.UnknownComputer, UnknownComputerAdmin)

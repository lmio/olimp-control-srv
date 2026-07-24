"""Create the single local management API administrator account."""

import os

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

USERNAME = "dev-admin"


class Command(BaseCommand):
    help = "Bootstrap the explicitly enabled local dev-admin account only"

    @transaction.atomic
    def handle(self, *args, **options):
        if settings.CTRL_PRODUCTION:
            raise CommandError("refusing to bootstrap a development admin in production")
        if os.environ.get("CTRL_BOOTSTRAP_DEV_ADMIN", "") != "1":
            raise CommandError(
                "refusing to bootstrap dev-admin without CTRL_BOOTSTRAP_DEV_ADMIN=1"
            )
        password = os.environ.get("CTRL_DEV_ADMIN_PASSWORD", "")
        if not password:
            raise CommandError("CTRL_DEV_ADMIN_PASSWORD must be set")

        User = get_user_model()
        user = User.objects.select_for_update().filter(username=USERNAME).first()
        if user is None:
            user = User(username=USERNAME)
        user.first_name = "Development"
        user.last_name = "Administrator"
        user.is_active = True
        user.is_staff = True
        user.is_superuser = True
        validate_password(password, user=user)
        user.set_password(password)
        user.full_clean()
        user.save()

        self.stdout.write(
            self.style.SUCCESS("Bootstrapped local dev-admin management account")
        )

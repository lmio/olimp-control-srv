"""Import the identifier-only Olimp-control contestant roster."""

import csv

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from ctrl.models import Student


class Command(BaseCommand):
    help = (
        "Upsert contestants from a UTF-8 CSV containing one identifier column. "
        "Computer mappings are imported separately in Django admin."
    )

    def add_arguments(self, parser):
        parser.add_argument("csv_path")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Validate the complete file without changing the database.",
        )

    def handle(self, *args, **options):
        identifiers = self._read_identifiers(options["csv_path"])
        if options["dry_run"]:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Validated {len(identifiers)} contestants; no changes written."
                )
            )
            return

        with transaction.atomic():
            existing = set(
                Student.objects.select_for_update()
                .filter(userid__in=identifiers)
                .values_list("userid", flat=True)
            )
            Student.objects.bulk_create(
                [
                    Student(userid=identifier)
                    for identifier in identifiers
                    if identifier not in existing
                ]
            )

        self.stdout.write(
            self.style.SUCCESS(f"Imported {len(identifiers)} contestants.")
        )

    def _read_identifiers(self, path):
        try:
            source = open(path, encoding="utf-8-sig", newline="")
        except OSError as error:
            raise CommandError(f"Cannot open CSV file: {error}") from error

        with source:
            reader = csv.DictReader(source)
            if tuple(reader.fieldnames or ()) != ("identifier",):
                raise CommandError("CSV headers must be exactly: identifier")

            identifiers = []
            seen = {}
            for line_number, row in enumerate(reader, start=2):
                if None in row:
                    raise CommandError(
                        f"Line {line_number}: unexpected extra CSV column."
                    )
                identifier = row.get("identifier")
                if (
                    not isinstance(identifier, str)
                    or not identifier
                    or identifier != identifier.strip()
                    or len(identifier) > 64
                ):
                    raise CommandError(
                        f"Line {line_number}: identifier must contain 1-64 "
                        "characters without outer whitespace."
                    )
                if identifier in seen:
                    raise CommandError(
                        f"Line {line_number}: duplicate identifier "
                        f"{identifier!r} (first seen on line "
                        f"{seen[identifier]})."
                    )
                seen[identifier] = line_number
                identifiers.append(identifier)

        if not identifiers:
            raise CommandError("CSV file contains no contestant rows.")
        return identifiers

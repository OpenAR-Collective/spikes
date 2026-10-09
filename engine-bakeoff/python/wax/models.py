# Adapted from the HiveAR prototype by Andy Troiano
# (https://github.com/atroiano/platform-design, commit 361b6aa,
# prototype/wax/models.py), Apache License 2.0.
# Modified by The OpenAR Collective contributors.
# Changes: added PendingEvent, the Pending Event Register row of WAX-0027,
# for the engine bake-off (Foundation-written). Andy's classes are unchanged.
from django.db import models

from wax.commands import require_command


class Event(models.Model):
    """Read-only mapping of core.event. Writes go through wax.store.append."""

    event_pk = models.UUIDField(primary_key=True)
    global_sequence_number = models.BigIntegerField()
    aggregate_sequence_number = models.IntegerField()
    event_type = models.TextField()
    schema_version = models.SmallIntegerField()
    aggregate_pk_ref = models.UUIDField()
    aggregate_type = models.TextField()
    occurred_at = models.DateTimeField()
    recorded_at = models.DateTimeField()
    actor_pk_ref = models.UUIDField()
    actor_type = models.TextField()
    prior_value = models.JSONField(null=True)
    new_value = models.JSONField(null=True)
    reason_code_fk = models.UUIDField(null=True)
    source = models.TextField(null=True)
    correlation_pk_ref = models.UUIDField(null=True)
    correlation_type = models.TextField(null=True)
    metadata = models.JSONField(null=True)
    prior_event_hash = models.TextField(null=True)
    event_hash = models.TextField()

    class Meta:
        managed = False
        db_table = 'core"."event'

    def save(self, *args, **kwargs):
        raise TypeError("core.event is append-only; use wax.store.append")

    def delete(self, *args, **kwargs):
        raise TypeError("core.event is append-only")


class GuardedQuerySet(models.QuerySet):
    def update(self, **kwargs):
        require_command()
        return super().update(**kwargs)

    def delete(self):
        require_command()
        return super().delete()

    def bulk_create(self, *args, **kwargs):
        require_command()
        return super().bulk_create(*args, **kwargs)

    def bulk_update(self, *args, **kwargs):
        require_command()
        return super().bulk_update(*args, **kwargs)


class GuardedModel(models.Model):
    """Authoritative state that only a command handler may write (WAX-0001)."""

    objects = GuardedQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        require_command()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        require_command()
        return super().delete(*args, **kwargs)


class AggregateRoot(GuardedModel):
    """State row for an aggregate root. Subclasses set aggregate_type.

    aggregate_sequence_number mirrors the last event written for the
    aggregate and is the expectedVersion passed to the store.
    """

    aggregate_type: str
    aggregate_sequence_number = models.PositiveIntegerField(default=0)

    class Meta:
        abstract = True


# Foundation-written for the engine bake-off (WORKLOADS.md, "Schema additions").
class PendingEvent(GuardedModel):
    """One entry in the Pending Event Register (WAX-0027): a future-dated
    event scheduled against one aggregate. Written only by the commands that
    schedule and fire it; the table is created by wax migration 0003."""

    pending_event_pk = models.UUIDField(primary_key=True)
    aggregate_pk_ref = models.UUIDField()
    aggregate_type = models.TextField()
    target_event_type = models.TextField()
    due_at = models.DateTimeField()
    status = models.TextField(default="pending")
    scheduled_by_event = models.UUIDField()
    fired_at = models.DateTimeField(null=True)

    class Meta:
        managed = False
        db_table = 'core"."pending_event'

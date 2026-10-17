"""Common abstract models."""
import uuid

from django.db import models


class BaseModel(models.Model):
    """UUID primary key + audit timestamps for every domain entity."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField("создано", auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField("обновлено", auto_now=True)

    class Meta:
        abstract = True
        ordering = ("-created_at",)

import datetime as dt

from sqlalchemy import DateTime
from sqlalchemy import Identity
from sqlalchemy import BigInteger
from sqlalchemy import func
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from app.db.engine import Base


class BaseMixin(Base):
    """Common columns for every table. Does not provide `id`."""

    __abstract__ = True

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, sort_order=-3
    )
    updated_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True, sort_order=-2
    )
    seq: Mapped[int] = mapped_column(BigInteger, Identity(start=1000), sort_order=-1)
    """Insertion order, and the cursor every list paginates on. 64-bit and never wraps."""


class BaseView(Base):
    """Read-only database view. Skipped by alembic autogenerate."""

    __abstract__ = True
    __allow_unmapped__ = True
    __table_args__ = {"info": {"skip_autogenerate": True}}

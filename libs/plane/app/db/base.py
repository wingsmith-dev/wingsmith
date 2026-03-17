import datetime as dt

from sqlalchemy import Integer
from sqlalchemy import DateTime
from sqlalchemy import Identity
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
    seq: Mapped[int] = mapped_column(Integer, Identity(start=1000, cycle=True), sort_order=-1)


class BaseView(Base):
    """Read-only database view. Skipped by alembic autogenerate."""

    __abstract__ = True
    __allow_unmapped__ = True
    __table_args__ = {"info": {"skip_autogenerate": True}}

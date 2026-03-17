from sqlalchemy import Select
from sqlalchemy import select

from app.core.ids import is_uid
from app.core.exceptions import Invalid


def parse_labels(raw: list[str] | None) -> dict[str, str]:
    """Turns repeated `label=k=v` query params into a selector."""
    selector: dict[str, str] = {}
    for item in raw or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise Invalid(f"label {item!r} is not k=v", {"label": item})
        selector[key] = value
    return selector


def ref_ids(model, ref: str, name_column: str = "name") -> Select:
    """The set of primary keys a path- or filter-level reference names.

    A reference is either an id or a name, per `contract/API.md` §3, and this is
    the only place that distinction is made. Expressed as a subquery rather than
    a lookup so that filtering by a related object stays one round trip.
    """
    column = model.uid if is_uid(ref) else getattr(model, name_column)
    return select(model.id).where(column == ref)

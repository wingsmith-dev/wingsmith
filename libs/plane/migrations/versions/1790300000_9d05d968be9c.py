"""policy attachments go when their subject is deleted

Revision ID: 9d05d968be9c
Revises: ee98c5cc5d50
Create Date: 2026-10-15 14:00:00.000000

`bind_policy_attachment.subject_id` is polymorphic and has no foreign key, so deleting a
tenant, project, cluster, endpoint or workload left its attachments behind, and policy
reads returned them with a null subject.

Each subject table gets an `AFTER DELETE` trigger that deletes the attachments naming
the row, which also covers rows removed by a cascade. Attachments whose subject is
already gone are deleted here.

`SUBJECTS` is a frozen copy of the attachable types at this revision. A new subject type
needs its own trigger in a new revision; `tests/test_models.py` fails until it has one.
"""

from collections.abc import Sequence

from alembic import op


revision: str = "9d05d968be9c"
down_revision: str | None = "ee98c5cc5d50"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SUBJECTS = {
    "tenant": "bind_tenant",
    "project": "bind_project",
    "cluster": "supply_cluster",
    "endpoint": "demand_endpoint",
    "workload": "demand_workload",
}

FUNCTION = "bind_policy_attachment_detach"


def _trigger(table: str) -> str:
    return f"trg_{table}_detach_policies"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE FUNCTION {FUNCTION}() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            DELETE FROM bind_policy_attachment
             WHERE subject_type = TG_ARGV[0] AND subject_id = OLD.id;
            RETURN NULL;
        END
        $$
        """
    )
    for subject_type, table in SUBJECTS.items():
        op.execute(
            f"DELETE FROM bind_policy_attachment a WHERE a.subject_type = '{subject_type}'"
            f" AND NOT EXISTS (SELECT 1 FROM {table} s WHERE s.id = a.subject_id)"
        )
        op.execute(
            f"CREATE TRIGGER {_trigger(table)} AFTER DELETE ON {table}"
            f" FOR EACH ROW EXECUTE FUNCTION {FUNCTION}('{subject_type}')"
        )


def downgrade() -> None:
    for table in SUBJECTS.values():
        op.execute(f"DROP TRIGGER {_trigger(table)} ON {table}")
    op.execute(f"DROP FUNCTION {FUNCTION}()")

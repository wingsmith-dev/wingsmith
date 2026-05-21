"""public model slug on endpoints

Revision ID: c5e9a2b73f41
Revises: a3c7e1f45b92
Create Date: 2026-09-01 06:00:00.000000

The model id a client sends was the endpoint's name, and the endpoint's name is
deliberately not patchable — it is the internal handle, it sits in URLs, metrics
labels and every link the console holds, and an endpoint exists precisely so that
deployments can be replaced without anybody noticing. Those are two different jobs
for one string, and the public one lost: a K3 endpoint made for a latency test was
called `kimi-k3-overhead-test`, so that, and not `moonshotai/kimi-k3`, was the only
thing `model` could say.

`model_slug` splits them. Null means the name still stands, so this changes nothing
on its own. Where it is set it becomes what the resolved table reports as the
endpoint's name, which is the key the gateway builds its route map on — so a public
model id is now a plane-side write and one gateway poll, with no engine flag, no pod
respec and nothing on any cluster.

Indexed and not unique. `model` must resolve to exactly one endpoint, but the
scoping question is open — `Endpoint.name` is globally unique only while there is
one tenant, and this column has to follow whatever that becomes rather than commit
to global uniqueness ahead of it.

"""

from typing import Union
from typing import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "c5e9a2b73f41"
down_revision: Union[str, None] = "a3c7e1f45b92"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("demand_endpoint", sa.Column("model_slug", sa.String(length=255), nullable=True))
    op.create_index("ix_demand_endpoint_model_slug", "demand_endpoint", ["model_slug"])


def downgrade() -> None:
    op.drop_index("ix_demand_endpoint_model_slug", table_name="demand_endpoint")
    op.drop_column("demand_endpoint", "model_slug")

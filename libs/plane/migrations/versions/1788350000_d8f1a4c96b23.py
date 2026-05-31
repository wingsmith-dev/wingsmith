"""frontend image on the recipe

Revision ID: d8f1a4c96b23
Revises: 6c29bbd8d370
Create Date: 2026-09-02 11:53:20.000000

A cluster ran one Dynamo frontend and every model was served through it, which made
the frontend image a fleet-wide lowest common denominator: one binary had to tokenize
and register everything on the cluster. On 2026-09-01 a model arrived that it could
not. Kimi K3's worker loaded 191.74 GiB across eight B300s on the stock
`vllm-runtime:1.4.0-patched.1` and served perfectly, while the 1.4.1 frontend
looped on `Unsupported tiktoken model_type 'kimi_k3'` and answered `/v1/models` with
an empty list. The model was healthy and unreachable, and the only way to reach it
was to move the image every other model on that cluster was served through.

So the frontend becomes one per model, and this column is what a model says about the
one it gets. It is a second image and not a reuse of `engine_image` because the two
are separately versioned binaries: the K3 fix is compiled into the frontend and the
worker correctly stays on the base image, which a single field cannot express.

Nullable, no backfill, no derivation. Null means the cluster decides — the operator
knows what that cluster runs its frontends on and the plane does not, so an address
invented here would be an `ImagePullBackOff` rather than a default. Every existing row
is null, which is the singleton's behaviour, so this migration changes nothing on its
own.

"""

from typing import Union
from typing import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "d8f1a4c96b23"
down_revision: Union[str, None] = "6c29bbd8d370"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("demand_recipe", sa.Column("frontend_image", sa.String(length=512), nullable=True))


def downgrade() -> None:
    op.drop_column("demand_recipe", "frontend_image")

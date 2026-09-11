"""Enable pgvector extension

Revision ID: 88cdef47b355
Revises: fe56fa70289e
Create Date: 2026-09-12 00:53:51.775621

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = "88cdef47b355"
down_revision = "fe56fa70289e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS vector")

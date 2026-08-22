"""Drop the user's display name.

It was collected on Settings and auto-filled from the first uploaded report,
but never rendered anywhere — not the nav, not the dashboard, not a greeting.
A username already answers "who is this account", so the second name was a
field to maintain with nothing on the other end of it.

Dropping the column rather than hiding the field: a value nothing reads and
nothing writes is worse than no column, because the next person to find it has
to work out which of the two names is the real one.

Revision ID: b9d5c30e17af
Revises: f8b2461d0a97
"""
from alembic import op
import sqlalchemy as sa


revision = "b9d5c30e17af"
down_revision = "f8b2461d0a97"
branch_labels = None
depends_on = None


def upgrade():
    # SQLite can't drop a column in place; batch mode rebuilds the table.
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.drop_column("name")


def downgrade():
    # Nullable on the way back: the values are gone, and the report upload that
    # used to repopulate them no longer writes here.
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.add_column(sa.Column("name", sa.String(length=120), nullable=True))

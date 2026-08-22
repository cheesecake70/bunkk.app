"""Breaks in the weekly timetable.

A slot becomes either a class or a break, so the grid can show the day as it
actually runs. Breaks carry no subject, which is why `subject_id` has to become
nullable — and why `active_slots` filters on `kind` before the engine ever sees
the grid.

Existing rows are all classes, so they backfill to kind='class'.

Revision ID: d5e93c17b608
Revises: c3d81b5a7e42
"""
from alembic import op
import sqlalchemy as sa


revision = "d5e93c17b608"
down_revision = "c3d81b5a7e42"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("timetable_slot", schema=None) as batch_op:
        # server_default backfills the existing rows; autogenerate omits it and
        # the NOT NULL then fails on any non-empty table.
        batch_op.add_column(sa.Column("kind", sa.String(length=10),
                                      nullable=False, server_default="class"))
        batch_op.add_column(sa.Column("label", sa.String(length=60), nullable=True))
        batch_op.alter_column("subject_id", existing_type=sa.Integer(), nullable=True)


def downgrade():
    # A break has no subject, so it cannot survive subject_id going NOT NULL.
    op.execute("DELETE FROM timetable_slot WHERE kind = 'break'")
    with op.batch_alter_table("timetable_slot", schema=None) as batch_op:
        batch_op.alter_column("subject_id", existing_type=sa.Integer(), nullable=False)
        batch_op.drop_column("label")
        batch_op.drop_column("kind")

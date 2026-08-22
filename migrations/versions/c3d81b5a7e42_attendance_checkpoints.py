"""Attendance checkpoints.

Colleges audit attendance before each exam, not only at the end of term. A
checkpoint is just a date: it reuses the same limits as everything else, and
what it changes is the horizon every projection runs to.

The semester end is the implicit final checkpoint and is NOT stored here —
`semester.end_date` stays its single source of truth.

Revision ID: c3d81b5a7e42
Revises: a1c7f4e29d30
"""
from alembic import op
import sqlalchemy as sa


revision = "c3d81b5a7e42"
down_revision = "a1c7f4e29d30"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "checkpoint",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("semester_id", sa.Integer(), nullable=False),
        sa.Column("on_date", sa.Date(), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(["semester_id"], ["semester.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("semester_id", "on_date", name="uq_checkpoint_date"),
    )
    with op.batch_alter_table("checkpoint", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_checkpoint_semester_id"),
                              ["semester_id"], unique=False)


def downgrade():
    with op.batch_alter_table("checkpoint", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_checkpoint_semester_id"))
    op.drop_table("checkpoint")

"""Guesses at how unmarked lectures will resolve.

A student with fifty NU lectures gets a worst-case number that is technically
honest and practically useless. This lets them say what those lectures will
probably be, and the guess feeds the real figures.

Nothing here needs cleaning up when the college catches up: predictions are read
through a join that requires the lecture to still be unknown, so a fresh report
retires them automatically and the row survives as a record of what was guessed.

Revision ID: f8b2461d0a97
Revises: e7a4092fc51d
"""
from alembic import op
import sqlalchemy as sa


revision = "f8b2461d0a97"
down_revision = "e7a4092fc51d"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "lecture_prediction",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("lecture_id", sa.Integer(), nullable=False),
        sa.Column("predicted", sa.String(length=1), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["lecture_id"], ["lecture_instance.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "lecture_id", name="uq_prediction_per_lecture"),
    )
    with op.batch_alter_table("lecture_prediction", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_lecture_prediction_user_id"),
                              ["user_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_lecture_prediction_lecture_id"),
                              ["lecture_id"], unique=False)


def downgrade():
    with op.batch_alter_table("lecture_prediction", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_lecture_prediction_lecture_id"))
        batch_op.drop_index(batch_op.f("ix_lecture_prediction_user_id"))
    op.drop_table("lecture_prediction")

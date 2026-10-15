"""Onboarding: limits chosen, end date marked as a guess.

Existing accounts start with `setup_done_at` empty, which is the point:
everyone already signed up walks through the quick setup on their next visit.

Revision ID: c1e8a5d27f43
Revises: a7c2e5f08d13
"""
from alembic import op
import sqlalchemy as sa


revision = "c1e8a5d27f43"
down_revision = "a7c2e5f08d13"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.add_column(sa.Column("setup_done_at", sa.DateTime(), nullable=True))

    with op.batch_alter_table("semester", schema=None) as batch_op:
        batch_op.add_column(sa.Column("end_date_is_guess", sa.Boolean(),
                                      nullable=False, server_default=sa.false()))


def downgrade():
    with op.batch_alter_table("semester", schema=None) as batch_op:
        batch_op.drop_column("end_date_is_guess")

    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.drop_column("setup_done_at")

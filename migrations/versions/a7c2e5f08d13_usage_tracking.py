"""Usage tracking: when people sign in, when they come back, what they use.

Two timestamps on the user and one thin event table. Existing accounts start
with both timestamps empty and fill them in on their next visit.

Revision ID: a7c2e5f08d13
Revises: b2d6f08a4c71
"""
from alembic import op
import sqlalchemy as sa


revision = "a7c2e5f08d13"
down_revision = "b2d6f08a4c71"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.add_column(sa.Column("last_login_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("last_seen_at", sa.DateTime(), nullable=True))

    op.create_table(
        "event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("event", schema=None) as batch_op:
        batch_op.create_index("ix_event_user_id", ["user_id"], unique=False)
        batch_op.create_index("ix_event_name_created_at", ["name", "created_at"],
                              unique=False)


def downgrade():
    with op.batch_alter_table("event", schema=None) as batch_op:
        batch_op.drop_index("ix_event_name_created_at")
        batch_op.drop_index("ix_event_user_id")
    op.drop_table("event")

    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.drop_column("last_seen_at")
        batch_op.drop_column("last_login_at")

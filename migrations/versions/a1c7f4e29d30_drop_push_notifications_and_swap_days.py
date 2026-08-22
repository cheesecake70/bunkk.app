"""Drop push notifications and swap-days.

Notifications and the PWA are gone, so the subscription table and the two
Settings columns that drove the morning brief go with them. Swap-days go too:
the concept only ever existed as a `kind` on Holiday, and a holiday list that
means exactly one thing is easier to reason about than one that means two.

Swap rows are deleted rather than converted. A swap said "run another weekday's
timetable here", which has no holiday equivalent — silently turning it into a
day off would invent absences the student never planned.

Revision ID: a1c7f4e29d30
Revises: 4b314397b9a2
"""
from alembic import op
import sqlalchemy as sa


revision = "a1c7f4e29d30"
down_revision = "4b314397b9a2"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table("push_subscription")

    with op.batch_alter_table("settings", schema=None) as batch_op:
        batch_op.drop_column("notify_enabled")
        batch_op.drop_column("notify_hour")

    # A swap is not a holiday; drop the rows before the columns that define them.
    op.execute("DELETE FROM holiday WHERE kind = 'swap'")
    with op.batch_alter_table("holiday", schema=None) as batch_op:
        batch_op.drop_column("swap_weekday")
        batch_op.drop_column("kind")


def downgrade():
    with op.batch_alter_table("holiday", schema=None) as batch_op:
        batch_op.add_column(sa.Column("kind", sa.String(length=10),
                                      nullable=False, server_default="holiday"))
        batch_op.add_column(sa.Column("swap_weekday", sa.Integer(), nullable=True))

    with op.batch_alter_table("settings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("notify_enabled", sa.Boolean(),
                                      nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column("notify_hour", sa.Integer(),
                                      nullable=False, server_default="7"))

    op.create_table(
        "push_subscription",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("endpoint", sa.String(length=500), nullable=False),
        sa.Column("p256dh", sa.String(length=200), nullable=False),
        sa.Column("auth", sa.String(length=100), nullable=False),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_sent_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("endpoint"),
    )
    with op.batch_alter_table("push_subscription", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_push_subscription_user_id"),
                              ["user_id"], unique=False)

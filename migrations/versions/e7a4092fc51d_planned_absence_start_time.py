"""Plan a single lecture, not just a subject-day.

A timetable can run two lectures of one subject in a day. Keying planned
absences on (user, date, subject) made those two indistinguishable, so the
second could never be planned. Adding start_time gives the row three tiers:
whole day, whole subject that day, or exactly one lecture.

Existing rows get start_time = NULL, which means "every lecture of that subject
that day" — a superset of what they meant before, so nothing needs backfilling.

Revision ID: e7a4092fc51d
Revises: d5e93c17b608
"""
from alembic import op
import sqlalchemy as sa


revision = "e7a4092fc51d"
down_revision = "d5e93c17b608"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("planned_absence", schema=None) as batch_op:
        batch_op.add_column(sa.Column("start_time", sa.Time(), nullable=True))
        batch_op.create_unique_constraint(
            "uq_planned_absence", ["user_id", "on_date", "subject_id", "start_time"]
        )


def downgrade():
    # Two rows differing only by start_time collapse to one without it.
    op.execute("""
        DELETE FROM planned_absence WHERE id NOT IN (
            SELECT MIN(id) FROM planned_absence
            GROUP BY user_id, on_date, subject_id
        )
    """)
    with op.batch_alter_table("planned_absence", schema=None) as batch_op:
        batch_op.drop_constraint("uq_planned_absence", type_="unique")
        batch_op.drop_column("start_time")

"""One whole-day absence per day, one subject-wide absence per subject-day.

`uq_planned_absence` covers (user, date, subject, start_time), but NULLs are
distinct in a unique index — so it never stopped two whole-day rows (subject
and start both NULL) or two subject-wide rows (start NULL). The application
checked before inserting, which holds for one request and not for two at once:
a double tap could store both, and every later request for that day then
failed on finding two rows where it expected one.

Partial unique indexes close each NULL tier. Duplicates already stored are
collapsed to the oldest row first, and a whole-day row that somehow carries a
start time loses it — the time never meant anything there.

Revision ID: a9c4e17b3d52
Revises: f3b8d2a91c04
"""
from alembic import op
import sqlalchemy as sa


revision = "a9c4e17b3d52"
down_revision = "f3b8d2a91c04"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        DELETE FROM planned_absence
        WHERE subject_id IS NULL AND id NOT IN (
            SELECT MIN(id) FROM planned_absence
            WHERE subject_id IS NULL
            GROUP BY user_id, on_date
        )
    """)
    op.execute("UPDATE planned_absence SET start_time = NULL WHERE subject_id IS NULL")
    op.execute("""
        DELETE FROM planned_absence
        WHERE subject_id IS NOT NULL AND start_time IS NULL AND id NOT IN (
            SELECT MIN(id) FROM planned_absence
            WHERE subject_id IS NOT NULL AND start_time IS NULL
            GROUP BY user_id, on_date, subject_id
        )
    """)
    op.create_index(
        "uq_absence_whole_day", "planned_absence", ["user_id", "on_date"],
        unique=True,
        sqlite_where=sa.text("subject_id IS NULL"),
        postgresql_where=sa.text("subject_id IS NULL"),
    )
    op.create_index(
        "uq_absence_subject_day", "planned_absence",
        ["user_id", "on_date", "subject_id"],
        unique=True,
        sqlite_where=sa.text("subject_id IS NOT NULL AND start_time IS NULL"),
        postgresql_where=sa.text("subject_id IS NOT NULL AND start_time IS NULL"),
    )


def downgrade():
    op.drop_index("uq_absence_subject_day", table_name="planned_absence")
    op.drop_index("uq_absence_whole_day", table_name="planned_absence")

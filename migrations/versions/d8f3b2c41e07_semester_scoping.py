"""Scope the ledger to the active semester.

A report from a new semester used to create a second Semester row and then
carry on counting last term's lectures in this term's totals, because every
dashboard query was keyed on the user alone. Snapshots now remember which
semester their header named, so coverage is judged per term as well.

Existing snapshots are backfilled onto their owner's newest semester, which is
the only one anyone had when this runs. Two columns nothing read or wrote go
with it, and the semester foreign keys the app filters on gain indexes.

Revision ID: d8f3b2c41e07
Revises: c7f1a4b82e50
"""
from alembic import op
import sqlalchemy as sa


revision = "d8f3b2c41e07"
down_revision = "c7f1a4b82e50"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("report_snapshot", schema=None) as batch_op:
        batch_op.add_column(sa.Column("semester_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_report_snapshot_semester_id_semester", "semester",
            ["semester_id"], ["id"],
        )
        batch_op.create_index(batch_op.f("ix_report_snapshot_semester_id"),
                              ["semester_id"], unique=False)

    op.execute(sa.text(
        "UPDATE report_snapshot SET semester_id = ("
        "  SELECT id FROM semester WHERE semester.user_id = report_snapshot.user_id"
        "  ORDER BY is_active DESC, id DESC LIMIT 1"
        ") WHERE semester_id IS NULL"
    ))

    with op.batch_alter_table("subject", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_subject_semester_id"),
                              ["semester_id"], unique=False)
    with op.batch_alter_table("timetable_version", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_timetable_version_semester_id"),
                              ["semester_id"], unique=False)

    with op.batch_alter_table("settings", schema=None) as batch_op:
        batch_op.drop_column("advanced_mode")
    with op.batch_alter_table("semester", schema=None) as batch_op:
        batch_op.drop_column("start_date")


def downgrade():
    with op.batch_alter_table("semester", schema=None) as batch_op:
        batch_op.add_column(sa.Column("start_date", sa.Date(), nullable=True))
    with op.batch_alter_table("settings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("advanced_mode", sa.Boolean(), nullable=False,
                                      server_default=sa.false()))
    with op.batch_alter_table("timetable_version", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_timetable_version_semester_id"))
    with op.batch_alter_table("subject", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_subject_semester_id"))
    with op.batch_alter_table("report_snapshot", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_report_snapshot_semester_id"))
        batch_op.drop_constraint("fk_report_snapshot_semester_id_semester",
                                 type_="foreignkey")
        batch_op.drop_column("semester_id")

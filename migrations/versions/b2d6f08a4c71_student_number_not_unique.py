"""A student number may back more than one account.

`uq_user_student_number` made a student number belong to the first account
that uploaded it; anyone else uploading the same report was refused. That
rule is gone: starting a fresh account and uploading your own report into it
is an ordinary thing to do, and each account keeps its own separate ledger
either way. The column stays — it still stops one account mixing two
students' reports.

Revision ID: b2d6f08a4c71
Revises: a9c4e17b3d52
"""
from alembic import op


revision = "b2d6f08a4c71"
down_revision = "a9c4e17b3d52"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.drop_constraint("uq_user_student_number", type_="unique")


def downgrade():
    # Fails if two accounts share a number by now, which is the honest
    # answer: the old rule cannot be put back over data that breaks it.
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.create_unique_constraint("uq_user_student_number", ["student_number"])

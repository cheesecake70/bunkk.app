"""Email verification at sign-up.

A new account proves its address by clicking a mailed link before it can sign
in. Everyone who already has an account was using it before this existed, so
they are marked verified as of this migration rather than locked out.

Revision ID: e2a9c7d15b40
Revises: d8f3b2c41e07
"""
from alembic import op
import sqlalchemy as sa


revision = "e2a9c7d15b40"
down_revision = "d8f3b2c41e07"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.add_column(sa.Column("email_verified_at", sa.DateTime(), nullable=True))
    op.execute(sa.text("UPDATE user SET email_verified_at = CURRENT_TIMESTAMP"))


def downgrade():
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.drop_column("email_verified_at")

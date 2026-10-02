"""Google sign-in replaces passwords.

Adds the Google subject id and drops everything that only existed because
there were passwords: the hash, the failed-login counter and the lockout.
Existing accounts keep their rows; the first Google sign-in with the same
address attaches the `sub` (see auth._user_for).

Revision ID: f3b8d2a91c04
Revises: e2a9c7d15b40
"""
from alembic import op
import sqlalchemy as sa


revision = "f3b8d2a91c04"
down_revision = "e2a9c7d15b40"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.add_column(sa.Column("google_sub", sa.String(length=64), nullable=True))
        batch_op.create_unique_constraint("uq_user_google_sub", ["google_sub"])
        batch_op.drop_column("password_hash")
        batch_op.drop_column("failed_logins")
        batch_op.drop_column("locked_until")


def downgrade():
    with op.batch_alter_table("user", schema=None) as batch_op:
        # Nobody can sign in with an empty hash; the downgrade restores the
        # column so the old code boots, not so the old logins work.
        batch_op.add_column(sa.Column("password_hash", sa.String(length=255),
                                      nullable=False, server_default=""))
        batch_op.add_column(sa.Column("failed_logins", sa.Integer(),
                                      nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("locked_until", sa.DateTime(), nullable=True))
        batch_op.drop_constraint("uq_user_google_sub", type_="unique")
        batch_op.drop_column("google_sub")

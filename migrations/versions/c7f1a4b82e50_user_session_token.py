"""Sessions carry a token, not the user's row id.

SQLite hands a deleted row's id straight to the next account created, and
Flask-Login's remember-me cookie holds whatever `get_id` returns. Together that
meant a cookie left over from a deleted account signed its holder into whoever
took that id next. The token is random, unique, and dies with the row.

Existing sessions all hold an integer, so everyone is signed out once when this
runs. That is the intended cost.

Revision ID: c7f1a4b82e50
Revises: b9d5c30e17af
"""
import secrets

from alembic import op
import sqlalchemy as sa


revision = "c7f1a4b82e50"
down_revision = "b9d5c30e17af"
branch_labels = None
depends_on = None


def upgrade():
    # NOT NULL UNIQUE, so existing rows need a value of their own first: add it
    # nullable, fill each row with its own token, then tighten. A server_default
    # would hand every row the same string and violate the unique index.
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.add_column(sa.Column("session_token", sa.String(length=64),
                                      nullable=True))

    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT id FROM user")).fetchall()
    for (user_id,) in rows:
        connection.execute(
            sa.text("UPDATE user SET session_token = :t WHERE id = :i"),
            {"t": secrets.token_urlsafe(32), "i": user_id},
        )

    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.alter_column("session_token", existing_type=sa.String(length=64),
                              nullable=False)
        batch_op.create_index(batch_op.f("ix_user_session_token"),
                              ["session_token"], unique=True)


def downgrade():
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_user_session_token"))
        batch_op.drop_column("session_token")

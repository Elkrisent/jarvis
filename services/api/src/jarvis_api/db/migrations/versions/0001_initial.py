"""Initial schema: SPEC §12 relational tables (Timescale tables come in M4.1).

Revision ID: 0001
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("email", sa.Text, nullable=False, unique=True),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("role IN ('admin', 'member')", name="ck_users_role"),
    )
    op.create_table(
        "projects",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("slug", sa.Text, nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    )
    op.create_table(
        "jarvis_keys",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column(
            "project_id",
            sa.Text,
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("prefix", sa.Text, nullable=False),
        sa.Column("hash", sa.Text, nullable=False, unique=True),
        sa.Column(
            "allowed_api_ids",
            postgresql.ARRAY(sa.Text),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("created_by", sa.Text, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_jarvis_keys_project_id", "jarvis_keys", ["project_id"])
    op.create_table(
        "api_dependencies",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column(
            "project_id",
            sa.Text,
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("slug", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("current_version_id", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.UniqueConstraint("project_id", "slug", name="uq_api_dependencies_project_slug"),
    )
    op.create_table(
        "api_policy_versions",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column(
            "api_id",
            sa.Text,
            sa.ForeignKey("api_dependencies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("config_json", postgresql.JSONB, nullable=False),
        sa.Column("diff_json", postgresql.JSONB),
        sa.Column("author_id", sa.Text, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("reason", sa.Text),
        # References ``recommendations``, which does not exist until a later module.
        sa.Column("recommendation_id", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.UniqueConstraint("api_id", "version", name="uq_api_policy_versions_api_version"),
        sa.CheckConstraint("version >= 1", name="ck_api_policy_versions_version"),
    )
    # The two tables reference each other, so this FK can only be added once both exist.
    op.create_foreign_key(
        "fk_api_dependencies_current_version",
        "api_dependencies",
        "api_policy_versions",
        ["current_version_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_table(
        "secrets",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("key_id", sa.Text, nullable=False),
        sa.Column("ciphertext", sa.LargeBinary, nullable=False),
        sa.Column("nonce", sa.LargeBinary, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value_json", postgresql.JSONB, nullable=False),
        sa.Column("updated_by", sa.Text, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("actor_id", sa.Text, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("target_type", sa.Text),
        sa.Column("target_id", sa.Text),
        sa.Column("detail_json", postgresql.JSONB),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    )
    op.create_index("ix_audit_log_at", "audit_log", ["at"])


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_table("settings")
    op.drop_table("secrets")
    op.drop_constraint(
        "fk_api_dependencies_current_version", "api_dependencies", type_="foreignkey"
    )
    op.drop_table("api_policy_versions")
    op.drop_table("api_dependencies")
    op.drop_table("jarvis_keys")
    op.drop_table("projects")
    op.drop_table("users")

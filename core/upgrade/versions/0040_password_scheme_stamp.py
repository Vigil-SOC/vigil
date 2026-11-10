"""Record when this install moved to the PBKDF2 password scheme (#4174)."""

from core.upgrade.revision import run_sql

revision = "0040_password_scheme_stamp"
down_revision = "0039_custom_agent_persona_drop"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )

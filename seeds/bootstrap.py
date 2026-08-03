"""Run every Phase 1 seed against the configured database.

Idempotent — safe to re-run after a migration or on a fresh clone::

    python -m seeds.bootstrap

Prints the bootstrap administrator's temporary password once, and only when a
brand-new administrator was actually created. It is never stored in plaintext
and never written to the audit log.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules.database import session_scope  # noqa: E402
from seeds import seed_reference_data, seed_roles_permissions, seed_term_templates  # noqa: E402

log = logging.getLogger(__name__)


def run() -> None:
    with session_scope() as session:
        seed_reference_data.run(session)
        seed_term_templates.run(session)
        user, temporary_password = seed_roles_permissions.run(session)

    print("\nSeeding complete.")
    if temporary_password:
        print("\n" + "=" * 68)
        print("  BOOTSTRAP ADMINISTRATOR CREATED")
        print("=" * 68)
        print(f"  Username:           {user.username}")
        print(f"  Temporary password: {temporary_password}")
        print()
        print("  Shown once and stored only as a bcrypt hash. You will be")
        print("  required to change it at first login.")
        print("=" * 68 + "\n")
    else:
        print("Users already existed; no bootstrap administrator was created.\n")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(name)s: %(message)s")
    run()

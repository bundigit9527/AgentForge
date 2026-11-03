"""Local operator provisioning. No default account, password or token is created."""

import argparse
import json
from getpass import getpass

from agentforge_core.identity import PermissionDenied
from agentforge_core.models import RunConflict, Scope
from agentforge_core.settings import Settings
from agentforge_storage.database import create_database
from agentforge_storage.identity import IdentityRepository
from agentforge_storage.passwords import PasswordLogin
from sqlalchemy.exc import SQLAlchemyError


def main():
    parser = argparse.ArgumentParser(description="Provision local AgentForge identities")
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("bootstrap", help="Create a new space and its administrator")
    issue = commands.add_parser("issue-token", help="Issue a token for an existing active member")
    password = commands.add_parser("set-password", help="Provision or reset native Web login")
    password.add_argument("--email", required=True)
    for command in (bootstrap, issue, password):
        command.add_argument("--tenant", required=True)
        command.add_argument("--space", required=True)
        command.add_argument("--user", required=True)
    bootstrap.add_argument("--tenant-name", required=True)
    bootstrap.add_argument("--space-name", required=True)
    bootstrap.add_argument("--display-name", required=True)
    issue.add_argument("--label", default="local access")
    issue.add_argument("--hours", type=float, default=24)
    args = parser.parse_args()
    engine = None
    try:
        settings = Settings.from_env()
        scope = Scope(args.tenant, args.space, args.user)
        engine = create_database(settings.database_url)
        identities = IdentityRepository(engine)
        if args.command == "bootstrap":
            identities.bootstrap(
                scope,
                tenant_name=args.tenant_name,
                space_name=args.space_name,
                display_name=args.display_name,
            )
            print("Space and administrator created; issue a token separately.")
        elif args.command == "set-password":
            secret = getpass("Web password (at least 12 characters): ")
            if secret != getpass("Confirm password: "):
                raise ValueError("Passwords do not match")
            PasswordLogin(identities).set_password(scope, args.email, secret)
            print("Web login configured; existing password sessions revoked.")
        else:
            result = identities.issue_token(
                scope, label=args.label, lifetime_seconds=args.hours * 3600
            )
            print(json.dumps(result))
    except (SQLAlchemyError, RunConflict, PermissionDenied, ValueError, EOFError):
        parser.exit(
            1, "Identity operation failed; check arguments, membership and database migrations.\n"
        )
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    main()

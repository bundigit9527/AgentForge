"""Start one local development service with only its required AgentForge configuration."""

import argparse
import os
import shutil
import sys
from pathlib import Path

from agentforge_connectors.browser import BrowserPolicy, service_configuration
from agentforge_runtime.config import ModelSettings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=("check", "api", "worker", "supervisor", "web"))
    parser.add_argument("--rakazo-root", type=Path)
    args = parser.parse_args()
    # The caller loads .env with uv. Never forward model/DB/MCP credentials to Web.
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("AGENTFORGE_") and k != "SCREEN_PROXY_SECRET"
    }
    shared = {
        "AGENTFORGE_DESKTOP_POOL",
        "AGENTFORGE_BROWSER_API_ORIGIN",
        "AGENTFORGE_BROWSER_TOKEN",
        "AGENTFORGE_COMPUTER_IMAGE",
        "AGENTFORGE_WORKSPACE_CHECKPOINTS",
        "AGENTFORGE_DESKTOP_LIMIT",
        "AGENTFORGE_TERMINAL_API_ORIGIN",
    }
    if args.service == "check":
        ModelSettings.from_env()
        service_configuration()
        policy = BrowserPolicy.from_env()
        if not policy.grants:
            parser.error("Configure explicit browser grants first")
        endpoint = Path(os.environ.get("AGENTFORGE_DESKTOP_POOL", ""))
        if not endpoint.is_absolute() or not endpoint.parent.is_dir():
            parser.error("Configure an absolute desktop pool path with an existing directory")
        if endpoint.parent.stat().st_mode & 0o077:
            parser.error("The desktop pool directory must have permissions 0700")
        screen = os.environ.get("SCREEN_PROXY_SECRET", "")
        if len(screen) < 32 or screen == os.environ.get("AGENTFORGE_BROWSER_TOKEN"):
            parser.error("Configure a separate screen proxy secret of at least 32 characters")
        print("Local model, browser grants, dedicated secrets and desktop paths are configured.")
        print("This checks configuration only; it does not start services or call the model.")
        return
    if args.service in {"api", "worker"}:
        env.update({k: v for k, v in os.environ.items() if k.startswith("AGENTFORGE_")})
        if args.service == "api":
            env.pop("AGENTFORGE_MODEL_API_KEY", None)
            env["SCREEN_PROXY_SECRET"] = os.environ["SCREEN_PROXY_SECRET"]
            command = [
                sys.executable,
                "-m",
                "uvicorn",
                "agentforge_api.app:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
                "--workers",
                "1",
            ]
        else:
            command = [sys.executable, "-m", "agentforge_worker.main", "--watch"]
    elif args.service == "supervisor":
        env.update({k: v for k, v in os.environ.items() if k in shared})
        command = [sys.executable, "-m", "agentforge_supervisor.desktop_pool"]
    else:
        root = args.rakazo_root
        if root is None or not (root / "apps/web/package.json").is_file():
            parser.error("Provide --rakazo-root pointing to the installed original Rakazo checkout")
        if not shutil.which("pnpm"):
            parser.error("pnpm is required by the original Rakazo Web")
        env.update(
            NODE_ENV="development",
            API_PROXY_TARGET="http://127.0.0.1:8000",
            WEB_PORT="5173",
            RAKAZO_DEV_HOST="127.0.0.1",
            SCREEN_PROXY_SECRET=os.environ["SCREEN_PROXY_SECRET"],
        )
        os.chdir(root)
        command = ["pnpm", "--filter", "@rakazo/web", "dev"]
    os.execvpe(command[0], command, env)


if __name__ == "__main__":
    main()

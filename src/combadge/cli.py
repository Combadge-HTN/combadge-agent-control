from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time

from .config import AgentConfig
from .daemon import serve


def _pid(config: AgentConfig) -> int | None:
    try: pid = int(config.pid_file.read_text())
    except (FileNotFoundError, ValueError): return None
    try: os.kill(pid, 0)
    except OSError: return None
    return pid


def start(config: AgentConfig) -> int:
    config.initialize()
    if _pid(config): print("Combadge agent daemon is already running."); return 0
    if not config.openai_key_file.exists():
        print(f"Missing OpenAI key file: {config.openai_key_file}", file=sys.stderr); return 2
    subprocess.Popen([sys.executable, "-m", "combadge.cli", "agents", "serve"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    for _ in range(600):
        if _pid(config) and config.socket.exists():
            token = config.ui_token_file.read_text().strip()
            print(f"Combadge agent daemon started. UI: http://127.0.0.1:{config.ui_port}/login?token={token}"); return 0
        time.sleep(.1)
    print("Daemon did not become ready.", file=sys.stderr); return 1


def stop(config: AgentConfig) -> int:
    pid = _pid(config)
    if not pid: print("Combadge agent daemon is not running."); return 1
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if not _pid(config): print("Combadge agent daemon stopped."); return 0
        time.sleep(.1)
    print("Daemon did not stop cleanly.", file=sys.stderr); return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="combadge"); sub = parser.add_subparsers(dest="command")
    sub.add_parser("start", help="start Combadge and ensure agent daemon is running")
    agents = sub.add_parser("agents", help="administer coding agents").add_subparsers(dest="agent_command")
    for name in ("start", "stop", "status", "serve"): agents.add_parser(name)
    args = parser.parse_args(argv); config = AgentConfig.from_env()
    if args.command == "start" or (args.command == "agents" and args.agent_command == "start"): return start(config)
    if args.command == "agents" and args.agent_command == "stop": return stop(config)
    if args.command == "agents" and args.agent_command == "status":
        pid = _pid(config); print(json.dumps({"running": bool(pid), "pid": pid, "socket": str(config.socket), "ui": f"http://127.0.0.1:{config.ui_port}"})); return 0 if pid else 1
    if args.command == "agents" and args.agent_command == "serve": serve(config); return 0
    parser.print_help(); return 2


if __name__ == "__main__": raise SystemExit(main())

"""Plan one Spec Kit operation and run it only after the plan is approved."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess


class SetupError(Exception):
    pass


AGENT_DIRS = {"codex": ".agents/skills", "claude": ".claude/skills"}
METADATA = {".specify/integration.json", ".specify/init-options.json"}
EVENT_OPTIONS = "--integration-options=--events false"


def inspect(path):
    """Check ancestors before looking at an entry. Missing paths are allowed."""
    for entry in (*reversed(path.parents), path):
        try:
            info = entry.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise SetupError(f"Linked path: {entry}")
        if entry != path and not stat.S_ISDIR(info.st_mode):
            raise SetupError(f"Expected a directory: {entry}")
    return info


def directory_identity(path):
    info = path.stat()
    return [info.st_dev, info.st_ino]


def parent_identity(path):
    parent = next(entry for entry in (path, *path.parents) if inspect(entry))
    return [str(parent), directory_identity(parent)]


def absolute(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise SetupError("Expected an absolute path with no parent traversal")
    if os.name == "nt" and (
        path.drive.startswith("\\") or any(unsafe_name(part) for part in path.parts[1:])
    ):
        raise SetupError("Expected a local Windows path with ordinary names")
    return path


def unsafe_name(part):
    return (
        part in {"", ".", ".."}
        or part[-1] in " ."
        or any(character in '<>:"\\|?*' or ord(character) < 32 for character in part)
        or re.fullmatch(
            r"CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[1-9¹²³]",
            part.split(".", 1)[0].rstrip().upper(),
        )
    )


def directory(value):
    path = absolute(value)
    info = inspect(path)
    if not info or not stat.S_ISDIR(info.st_mode) or path == path.parent:
        raise SetupError(f"Expected an existing working directory: {path}")
    return path


def executable(value):
    path = absolute(value)
    info = inspect(path)
    if not info or not stat.S_ISREG(info.st_mode):
        raise SetupError(f"Expected a vetted executable: {path}")
    if os.name == "nt" and path.suffix.lower() != ".exe":
        raise SetupError("Expected an executable application (.exe)")
    return path, [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def project_paths(root, paths, update, agent):
    result = {}
    for name in paths:
        parts = name.split("/")
        if any(unsafe_name(part) for part in parts):
            raise SetupError("Unsafe project destination")
        if parts[0].casefold() in {".git", ".ssh", ".aws"} or any(
            part.casefold().startswith(".env") for part in parts
        ):
            raise SetupError("Destination outside the setup scope")
        if update and name not in METADATA:
            raise SetupError("Only known Spec Kit metadata may be updated")
        if agent and not update and not (
            name.startswith(AGENT_DIRS[agent] + "/")
            or name == f".specify/integrations/{agent}.manifest.json"
        ):
            raise SetupError("Destination outside the integration scope")
        path = root.joinpath(*parts)
        info = inspect(path)
        if update:
            if not info or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SetupError(f"Expected ordinary metadata file: {name}")
            value = [info.st_ino, info.st_mtime_ns, digest(path.read_bytes())]
        else:
            if info:
                raise SetupError(f"Destination already exists: {name}")
            value = None
        result[name] = [parent_identity(path.parent), value]
    return result


def project_plan(args):
    workspace = directory(args.workspace)
    root = directory(args.root)
    if not root.is_relative_to(workspace) or root == Path.home():
        raise SetupError("Project is outside the approved workspace or is the user home")
    tool, identity = executable(args.executable)
    if tool.is_relative_to(workspace):
        raise SetupError("Use an executable outside the target workspace")
    if inspect(root / ".specify/integrations/packages.json") or inspect(
        root / ".specify/integrations/packages"
    ):
        raise SetupError("External adapter state requires file-based diagnosis")
    state_path = root / ".specify/integration.json"
    if args.operation == "init":
        for name in (".specify", ".codex/prompts", ".claude/commands"):
            if inspect(root / name):
                raise SetupError("Existing or partial Spec Kit setup requires diagnosis")
        if args.update or ".specify/integration.json" not in args.create:
            raise SetupError("Initialization needs a verified scaffold file list")
        command = [
            "init", "--here", "--non-interactive", "--integration", args.agent,
            "--script", "ps" if os.name == "nt" else "sh", EVENT_OPTIONS,
        ]
        if next(root.iterdir(), None) is not None:
            command.append("--force")
    else:
        state_info = inspect(state_path)
        if not state_info or not stat.S_ISREG(state_info.st_mode) or state_info.st_nlink != 1:
            raise SetupError("Missing or linked integration state")
        try:
            state = json.loads(state_path.read_bytes())
            registered = state["installed_integrations"]
            default = state["default_integration"]
            if not isinstance(registered, list) or default not in registered or any(
                key not in AGENT_DIRS for key in registered
            ):
                raise ValueError
        except (KeyError, ValueError, TypeError):
            raise SetupError("Invalid or unsupported integration state") from None
        if args.agent in registered:
            return {"status": "already-registered", "root": str(root), "agent": args.agent}
        if ".specify/integration.json" not in args.update or (
            f".specify/integrations/{args.agent}.manifest.json" not in args.create
        ):
            raise SetupError("Integration addition needs a verified file list")
        command = ["integration", "install", args.agent, EVENT_OPTIONS]

    names = [name.casefold() for name in args.create + args.update]
    if len(names) != len(set(names)):
        raise SetupError("Duplicate project destinations")
    creates = project_paths(root, args.create, False, args.agent if args.operation == "add" else None)
    updates = project_paths(root, args.update, True, None)
    return {
        "root": str(root), "identity": directory_identity(root),
        "command": [str(tool), *command], "executable_identity": identity,
        "create": sorted(creates), "update": sorted(updates), "before": creates | updates,
    }


def install_plan(args):
    root = directory(args.root)
    tool, identity = executable(args.executable)
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}", args.version):
        raise SetupError("Specify an exact verified release version")
    locations = {key: absolute(getattr(args, key)) for key in ("tools", "bin", "cache")}
    for path in locations.values():
        info = inspect(path)
        if path == path.parent or (info and not stat.S_ISDIR(info.st_mode)):
            raise SetupError(f"Invalid uv destination: {path}")
    installed = locations["tools"] / "specify-cli"
    launcher = locations["bin"] / ("specify.exe" if os.name == "nt" else "specify")
    if any(inspect(path) for path in (installed, launcher)):
        raise SetupError("Existing or incomplete Specify installation requires diagnosis")
    return {
        "root": str(root), "identity": directory_identity(root), "executable_identity": identity,
        "command": [
            str(tool), "--no-config", "tool", "install", f"specify-cli=={args.version}",
            "--default-index", "https://pypi.org/simple", "--no-build", "--no-python-downloads",
        ],
        "create": [str(installed), str(launcher)],
        "before": {name: parent_identity(path) for name, path in locations.items()},
        "environment": {f"UV_{key}": str(locations[name]) for key, name in (
            ("TOOL_DIR", "tools"), ("TOOL_BIN_DIR", "bin"), ("CACHE_DIR", "cache"),
        )},
    }


def digest(data):
    return hashlib.sha256(data).hexdigest()


def run(args):
    plan = install_plan(args) if args.task == "install-cli" else project_plan(args)
    if "command" not in plan:
        return plan
    keys = (
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE",
        "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "TMPDIR",
        "XDG_CONFIG_HOME", "XDG_DATA_HOME", "LANG", "LC_ALL",
    )
    env = {name: os.environ[name] for name in keys if name in os.environ}
    excluded = Path(args.workspace if args.task == "project" else args.root)
    env["PATH"] = os.pathsep.join(
        str(path) for entry in env.get("PATH", "").split(os.pathsep)
        if (path := Path(entry)).is_absolute() and not path.is_relative_to(excluded)
    )
    env.update(plan.get("environment", {}))
    env.update(
        NoDefaultCurrentDirectoryInExePath="1", PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1",
    )
    approval = digest(json.dumps([plan, env], sort_keys=True).encode())
    report = {key: value for key, value in plan.items() if key not in {
        "identity", "executable_identity", "before",
    }}
    if args.approve is None:
        return {"status": "plan", "plan_id": approval, **report}
    if args.approve != approval:
        raise SetupError("Plan changed or was not approved")
    result = subprocess.run(
        plan["command"], cwd=plan["root"], env=env, shell=False,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode:
        raise SetupError(
            f"{args.task} exited with code {result.returncode}. Inspect partial changes before retrying."
        )
    return {"status": "command-completed", **report}


def parser():
    cli = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = cli.add_subparsers(dest="task", required=True)
    install = commands.add_parser("install-cli", allow_abbrev=False)
    project = commands.add_parser("project", allow_abbrev=False)
    for command in (install, project):
        command.add_argument("--root", required=True)
        command.add_argument("--executable", required=True, help="Absolute path verified by the host")
        command.add_argument("--approve", help="Plan ID, supplied only after explicit consent")
    for argument in ("tools", "bin", "cache", "version"):
        install.add_argument(f"--{argument}", required=True)
    project.add_argument("--workspace", required=True)
    project.add_argument("--operation", required=True, choices=("init", "add"))
    project.add_argument("--agent", required=True, choices=AGENT_DIRS)
    for argument in ("create", "update"):
        project.add_argument(f"--{argument}", action="append", default=[])
    return cli


if __name__ == "__main__":
    try:
        print(json.dumps(run(parser().parse_args())))
    except SetupError as error:
        raise SystemExit(f"SpecKit-Start: {error}") from None
    except OSError:
        raise SystemExit("SpecKit-Start: filesystem or process operation failed") from None

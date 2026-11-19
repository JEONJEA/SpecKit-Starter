"""Plan one Spec Kit operation and run it only after the plan is approved."""

import argparse
import base64
import configparser
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tomllib
from email.parser import Parser
import socket
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler
import zipfile


class SetupError(Exception):
    pass


AGENT_DIRS = {"codex": ".agents/skills", "claude": ".claude/skills"}
INIT_OPTIONS = ".specify/init-options.json"
SHARED_MANIFEST = ".specify/integrations/speckit.manifest.json"
METADATA = {".specify/integration.json", INIT_OPTIONS, SHARED_MANIFEST}
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


def same_path(left, right):
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


def source_only(tool):
    """Make Python children compile the checked sources instead of trusting cached bytecode.

    A .pyc beside a module is loaded when its header matches the source's size and mtime,
    so it can differ from the source that was verified. Moving the cache below a regular
    file gives Python a location that cannot exist: nothing is read from it or written to it.
    """
    return {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPYCACHEPREFIX": str(tool / "no-bytecode")}


def read_regular(path, label):
    info = inspect(path)
    if not info or not stat.S_ISREG(info.st_mode):
        raise SetupError(f"Missing or linked {label}")
    try:
        return path.read_bytes()
    except OSError:
        raise SetupError(f"Could not read {label}") from None


def resolve_uv_launcher(path, bin_dir, tool_root, backing):
    if not path.is_relative_to(bin_dir):
        raise SetupError("Specify launcher is outside uv's reported tool bin directory")
    parent = directory(str(path.parent))
    if parent != path.parent:
        raise SetupError("Unexpected Specify launcher parent")
    try:
        info = path.lstat()
    except OSError:
        raise SetupError("Could not inspect resolved Specify launcher") from None
    linked = stat.S_ISLNK(info.st_mode) or (
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )
    if linked:
        try:
            target = path.resolve(strict=True)
        except OSError:
            raise SetupError("Could not resolve uv-managed Specify launcher") from None
        if not target.is_relative_to(tool_root.resolve()) or not same_path(target, backing):
            raise SetupError("Specify launcher link does not resolve to its uv-managed backing file")
        backing_info = inspect(target)
        if not backing_info or not stat.S_ISREG(backing_info.st_mode):
            raise SetupError("uv-managed Specify backing file is not regular")
        return target
    return executable(str(path))[0]


def site_packages(tool_root):
    if os.name == "nt":
        candidates = [tool_root / "Lib" / "site-packages"]
    else:
        lib = tool_root / "lib"
        info = inspect(lib)
        candidates = (
            [entry / "site-packages" for entry in lib.iterdir() if entry.name.startswith("python")]
            if info and stat.S_ISDIR(info.st_mode) else []
        )
    found = []
    for site in candidates:
        info = inspect(site)
        if not info or not stat.S_ISDIR(info.st_mode):
            continue
        for entry in site.iterdir():
            if entry.name.casefold().startswith("specify_cli-") and entry.name.casefold().endswith(".dist-info"):
                entry_info = inspect(entry)
                if entry_info and stat.S_ISDIR(entry_info.st_mode):
                    found.append(entry)
    if len(found) != 1:
        raise SetupError("Expected one Specify CLI distribution metadata directory")
    return found[0]


def receipt_index_source(tool_record):
    options = tool_record.get("options")
    if options is None:
        return "not-recorded"
    if not isinstance(options, dict):
        raise SetupError("Unsupported uv tool options metadata")
    sources = []
    for key, value in options.items():
        if "index" not in key.casefold() or value in (None, "", []):
            continue
        for item in value if isinstance(value, list) else [value]:
            url = item.get("url") if isinstance(item, dict) else item if isinstance(item, str) else None
            if not isinstance(url, str):
                raise SetupError("uv receipt has an unrecognized package index")
            parsed = urlsplit(url)
            if (parsed.scheme.casefold() != "https" or parsed.hostname is None or
                    parsed.hostname.casefold() != "pypi.org" or parsed.path.rstrip("/") != "/simple" or
                    parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise SetupError("uv receipt records a nonofficial package index")
            sources.append("pypi.org")
    return "pypi.org" if sources else "not-recorded"


def cached_wheel_source(cache_dir, version):
    """Confirm uv cached this exact wheel from the official PyPI file host."""
    cache_root = directory(cache_dir)
    buckets = [entry for entry in cache_root.iterdir() if re.fullmatch(r"wheels-v[0-9]+", entry.name)]
    records = []
    for bucket in buckets:
        info = inspect(bucket)
        if not info or not stat.S_ISDIR(info.st_mode):
            continue
        package_dir = bucket / "pypi" / "specify-cli"
        package_info = inspect(package_dir)
        if not package_info or not stat.S_ISDIR(package_info.st_mode):
            continue
        for record in package_dir.iterdir():
            if re.fullmatch(re.escape(version) + r"-[^/\\]+\.http", record.name):
                records.append(record)
    if not records:
        raise SetupError("No matching uv wheel cache source record")

    expected_prefix = f"specify_cli-{version}-".casefold()
    saw_official_wheel = False
    for record in records:
        data = read_regular(record, "uv wheel cache source record")
        if len(data) > 1024 * 1024:
            raise SetupError("uv wheel cache source record is unexpectedly large")
        urls = re.findall(rb"https?://[^\x00-\x20\"'<>]+", data)
        if not urls:
            raise SetupError("uv wheel cache source record has no package URL")
        for raw_url in urls:
            try:
                url = urlsplit(raw_url.decode("ascii").rstrip(".,;)]}"))
                port = url.port
            except (UnicodeDecodeError, ValueError):
                raise SetupError("Invalid URL in uv wheel cache source record") from None
            filename = url.path.rsplit("/", 1)[-1].casefold()
            if (url.scheme.casefold() != "https" or url.hostname is None or
                    url.username or url.password or port not in (None, 443)):
                raise SetupError("uv wheel cache records an insecure or ambiguous source")
            if (url.hostname.casefold() != "files.pythonhosted.org" or
                    not url.path.startswith("/packages/") or
                    not filename.startswith(expected_prefix) or not filename.endswith(".whl")):
                raise SetupError("uv wheel cache records a nonofficial or mismatched package source")
            saw_official_wheel = True
    if not saw_official_wheel:
        raise SetupError("uv wheel cache does not confirm the official package source")
    return "files.pythonhosted.org (uv wheel cache)"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def fetch_release_bytes(url, limit):
    try:
        with build_opener(NoRedirect()).open(url, timeout=20) as response:
            data = response.read(limit + 1)
    except URLError as error:
        if isinstance(error.reason, socket.gaierror):
            raise SetupError("DNS lookup failed while contacting " + urlsplit(url).hostname) from None
        raise
    if len(data) > limit:
        raise SetupError("PyPI response exceeds size limit")
    return data


# From the official 1.4.2 universal wheel (archive SHA-256
# 29e13231cb6b401205f979ec26ca10a3748f537fce76eb9437d877e19e23647d).
# Hash the sorted [wheel path, file SHA-256] pairs, excluding installer-rewritten RECORD.
PINNED_RELEASES = {"1.4.2": "ff967d8dac70f6d5380b8e6c6d0f76b39808128450fc2150da8ad84ecfc51e4e"}


def installed_release_digest(dist_info):
    site = dist_info.parent
    package = directory(str(site / "specify_cli"))
    files = [path for path in regular_files(package)
             if not (path.parent.name == "__pycache__" and path.suffix == ".pyc")]
    files.extend(dist_info / name for name in ("METADATA", "WHEEL", "entry_points.txt", "licenses/LICENSE"))
    rows = sorted([path.relative_to(site).as_posix(),
                   hashlib.sha256(read_regular(path, "installed wheel file")).hexdigest()] for path in files)
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def verify_release_files(dist_info, version):
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}", version):
        raise SetupError("Unsupported Specify release version")
    if version in PINNED_RELEASES:
        if installed_release_digest(dist_info) != PINNED_RELEASES[version]:
            raise SetupError("Installed files differ from pinned official wheel")
        return
    filename = f"specify_cli-{version}-py3-none-any.whl"
    try:
        release = json.loads(fetch_release_bytes(f"https://pypi.org/pypi/specify-cli/{version}/json", 2 << 20))
        artifact, = [item for item in release["urls"] if item["filename"] == filename]
        url = urlsplit(artifact["url"])
        if (url.scheme != "https" or url.netloc != "files.pythonhosted.org" or
                not url.path.startswith("/packages/") or url.path.split("/")[-1] != filename or
                url.query or url.fragment):
            raise SetupError("Unexpected PyPI wheel URL")
        wheel = fetch_release_bytes(url.geturl(), 32 << 20)
        if hashlib.sha256(wheel).hexdigest() != artifact["digests"]["sha256"]:
            raise SetupError("Official wheel digest mismatch")
        with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
            expected = set()
            for member in archive.infolist():
                if member.is_dir():
                    continue
                parts = member.filename.split("/")
                if parts[0] not in {"specify_cli", dist_info.name} or any(unsafe_name(p) for p in parts):
                    raise SetupError("Unsupported official wheel layout")
                if member.filename == dist_info.name + "/RECORD":
                    continue  # uv rewrites RECORD to include the generated launcher.
                path = dist_info.parent.joinpath(*parts)
                if read_regular(path, "installed wheel file") != archive.read(member):
                    raise SetupError(f"Installed file differs from official wheel: {member.filename}")
                expected.add(path)
            package = dist_info.parent / "specify_cli"
            for path in regular_files(package):
                if path not in expected and not (path.parent.name == "__pycache__" and path.suffix == ".pyc"):
                    raise SetupError("Unexpected file in installed Specify package")
    except (KeyError, TypeError, ValueError, OSError, zipfile.BadZipFile) as error:
        raise SetupError("Could not verify the official PyPI wheel") from error


def verify_uv_tool(args):
    """Corroborate a Specify launcher against uv's local tool receipt and wheel metadata."""
    bin_dir = directory(args.bin)
    tools_dir = directory(args.tools)
    tool_root = directory(str(tools_dir / "specify-cli"))
    if tool_root.parent != tools_dir:
        raise SetupError("Unexpected uv tool environment location")

    receipt_path = tool_root / "uv-receipt.toml"
    try:
        receipt = tomllib.loads(read_regular(receipt_path, "uv tool receipt").decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise SetupError("Invalid uv tool receipt") from None
    tool_record = receipt.get("tool")
    if not isinstance(tool_record, dict):
        raise SetupError("uv receipt has no tool record")
    index_source = receipt_index_source(tool_record)
    requirements = tool_record.get("requirements")
    if not isinstance(requirements, list) or not any(
        isinstance(item, dict) and str(item.get("name", "")).casefold() == "specify-cli"
        for item in requirements
    ):
        raise SetupError("uv receipt does not record specify-cli")
    entrypoints = tool_record.get("entrypoints")
    if not isinstance(entrypoints, list):
        raise SetupError("uv receipt has no Specify entrypoint")
    matching = [item for item in entrypoints if isinstance(item, dict) and
                str(item.get("name", "")).casefold() == "specify"]
    if len(matching) != 1:
        raise SetupError("uv receipt has an ambiguous Specify entrypoint")
    entry = matching[0]
    if str(entry.get("from", "")).casefold() != "specify-cli":
        raise SetupError("Specify entrypoint is not provided by specify-cli")
    install_path = entry.get("install-path")
    if not isinstance(install_path, str):
        raise SetupError("uv receipt has no absolute Specify launcher path")
    receipt_launcher = absolute(install_path)
    candidate = absolute(args.launcher) if args.launcher else receipt_launcher
    if os.path.normcase(os.path.abspath(candidate)) != os.path.normcase(os.path.abspath(receipt_launcher)):
        raise SetupError("Resolved Specify launcher differs from the uv receipt")
    backing = tool_root / ("Scripts/specify.exe" if os.name == "nt" else "bin/specify")
    launcher = resolve_uv_launcher(candidate, bin_dir, tool_root, backing)

    dist_info = site_packages(tool_root)
    try:
        metadata = Parser().parsestr(
            read_regular(dist_info / "METADATA", "Specify package metadata").decode("utf-8")
        )
    except UnicodeDecodeError:
        raise SetupError("Invalid Specify package metadata") from None
    if str(metadata.get("Name", "")).casefold() != "specify-cli":
        raise SetupError("Installed package metadata is not specify-cli")
    version = metadata.get("Version")
    if not version or (args.list_version and version != args.list_version):
        raise SetupError("uv tool list and package metadata versions do not match")
    if index_source == "not-recorded":
        if not args.cache:
            raise SetupError("uv receipt does not record the source; provide uv's cache directory")
        index_source = cached_wheel_source(args.cache, version)

    verify_release_files(dist_info, version)

    points = configparser.ConfigParser(interpolation=None)
    try:
        points.read_string(read_regular(dist_info / "entry_points.txt", "Specify entrypoint metadata").decode("utf-8"))
    except (UnicodeDecodeError, configparser.Error):
        raise SetupError("Invalid Specify entrypoint metadata") from None
    if not points.has_option("console_scripts", "specify") or points.get("console_scripts", "specify").strip() != "specify_cli:main":
        raise SetupError("Specify console entrypoint does not match specify-cli")

    backing_bytes = read_regular(backing, "uv-managed Specify launcher")
    launcher_bytes = read_regular(launcher, "resolved Specify launcher")
    try:
        record_text = read_regular(dist_info / "RECORD", "Specify package RECORD").decode("utf-8")
        record_rows = list(csv.reader(io.StringIO(record_text)))
    except (UnicodeDecodeError, csv.Error):
        raise SetupError("Invalid Specify package RECORD") from None
    site_root = dist_info.parent
    expected_record_path = os.path.relpath(backing, site_root).replace(os.sep, "/")
    verified_record = False
    for row in record_rows:
        if len(row) < 3 or row[0].replace("\\", "/") != expected_record_path:
            continue
        recorded_path = site_root / row[0]
        record_info = inspect(recorded_path)
        if not record_info or not stat.S_ISREG(record_info.st_mode) or not same_path(recorded_path, backing):
            raise SetupError("Specify package RECORD points to an unexpected launcher")
        algorithm, separator, encoded = row[1].partition("=")
        expected_size = row[2]
        actual_digest = base64.urlsafe_b64encode(hashlib.sha256(backing_bytes).digest()).decode("ascii").rstrip("=")
        if (separator and algorithm == "sha256" and encoded == actual_digest and
                expected_size.isdecimal() and int(expected_size) == len(backing_bytes)):
            verified_record = True
        break
    if not verified_record:
        raise SetupError("uv-managed launcher does not match its package RECORD")
    if hashlib.sha256(launcher_bytes).digest() != hashlib.sha256(backing_bytes).digest():
        raise SetupError("Resolved Specify launcher differs from the uv-managed launcher")
    return {
        "status": "verified",
        "package": "specify-cli",
        "version": version,
        "launcher": str(launcher),
        "backing": str(backing),
        "index_source": index_source,
        "signature_required": False,
        # Launch Specify with these so the files compared above are the code that runs.
        "environment": source_only(launcher),
        # RECORD sits beside the launcher it describes, so that match shows a consistent
        # install, not an untampered one. Nothing below is compared with an official copy.
        "not_content_verified": ["launcher executable", "Python interpreter", "dependencies"],
    }


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


def regular_files(root):
    for path in root.iterdir():
        info = inspect(path)
        if info and stat.S_ISDIR(info.st_mode):
            yield from regular_files(path)
        elif info and stat.S_ISREG(info.st_mode):
            yield path
        else:
            raise SetupError("Expected only ordinary files and directories")


def read_metadata(root, name):
    path = root / name
    info = inspect(path)
    if not info or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise SetupError(f"Missing or linked metadata: {name}")
    try:
        data = json.loads(path.read_bytes())
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    raise SetupError(f"Invalid metadata: {name}")


def verify_preview(args, workspace, root, default=None):
    if not args.preview:
        raise SetupError("A verified scratch preview is required")
    preview = directory(args.preview)
    if preview.is_relative_to(workspace) or workspace.is_relative_to(preview):
        raise SetupError("Use a private scratch preview outside the workspace")
    files = {path.relative_to(preview).as_posix() for path in regular_files(preview)}
    if args.operation == "init":
        if files != set(args.create):
            raise SetupError("Initialization destinations do not match the scratch preview")
        return {}

    prefix = AGENT_DIRS[args.agent] + "/"
    manifest = f".specify/integrations/{args.agent}.manifest.json"
    if {name for name in files if name.startswith(prefix) or name == manifest} != set(args.create):
        raise SetupError("Integration destinations do not match the scratch preview")

    shared = read_metadata(root, SHARED_MANIFEST)
    reference = read_metadata(preview, SHARED_MANIFEST)
    options = read_metadata(root, INIT_OPTIONS)
    preview_options = read_metadata(preview, INIT_OPTIONS)
    tracked = reference.get("files")
    version = reference.get("version")
    if (
        not isinstance(tracked, dict) or not tracked or not isinstance(version, str)
        or not isinstance(shared.get("files"), dict) or shared["files"].keys() != tracked.keys()
        or not tracked.keys() <= files or shared.get("version") != version
        or options.get("speckit_version") != version or preview_options.get("speckit_version") != version
        or options.get("ai") != default or preview_options.get("ai") != default
        or options.get("script") not in ("sh", "ps", "py")
        or options.get("script") != preview_options.get("script")
    ):
        raise SetupError("Shared infrastructure differs from the matching CLI preview")
    before = {}
    for name in tracked:
        path = root / name
        info = inspect(path)
        if not info or not stat.S_ISREG(info.st_mode):
            raise SetupError(f"Missing shared infrastructure: {name}")
        before[name] = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]
    if os.name != "nt":
        for name in ("scripts", "extensions"):
            folder = root / ".specify" / name
            if inspect(folder):
                for path in regular_files(folder):
                    info = path.lstat()
                    if path.suffix == ".sh" and not info.st_mode & 0o111:
                        with path.open("rb") as stream:
                            if stream.read(2) == b"#!":
                                raise SetupError("Integration would change existing script permissions")
                    before[path.relative_to(root).as_posix()] = [
                        info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode,
                    ]
    return before


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
    default = None
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
        state = read_metadata(root, ".specify/integration.json")
        try:
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
        if set(args.update) != METADATA or (
            f".specify/integrations/{args.agent}.manifest.json" not in args.create
        ):
            raise SetupError("Integration addition needs a verified file list")
        command = ["integration", "install", args.agent, EVENT_OPTIONS]

    names = [name.casefold() for name in args.create + args.update]
    if len(names) != len(set(names)):
        raise SetupError("Duplicate project destinations")
    creates = project_paths(root, args.create, False, args.agent if args.operation == "add" else None)
    updates = project_paths(root, args.update, True, None)
    shared = verify_preview(args, workspace, root, default)
    return {
        "root": str(root), "identity": directory_identity(root),
        "command": [str(tool), *command], "executable_identity": identity,
        "create": sorted(creates), "update": sorted(updates), "before": shared | creates | updates,
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


def plan_id(plan, env):
    """Fingerprint the operation and the child environment as separate halves."""
    operation = digest(json.dumps(plan, sort_keys=True).encode())[:32]
    environment = digest(json.dumps(env, sort_keys=True).encode())[:32]
    return f"{operation}-{environment}"


def approval_mismatch(approved, current):
    if not re.fullmatch(r"[0-9a-f]{32}-[0-9a-f]{32}", approved):
        return "Plan changed or was not approved: the approval is not a plan ID from this guard"
    operation, environment = approved.split("-")
    current_operation, current_environment = current.split("-")
    if operation != current_operation and environment != current_environment:
        reason = "the command, target files and child environment all differ from the approved plan"
    elif operation != current_operation:
        reason = "the command, arguments or target file state differ from the approved plan"
    else:
        reason = (
            "the child environment (such as PATH or TEMP) differs from the approved plan. "
            "Run the plan and apply with the same environment"
        )
    return f"Plan changed or was not approved: {reason}. Create a new plan and obtain approval again"


def latest_stable():
    try:
        project = json.loads(fetch_release_bytes("https://pypi.org/pypi/specify-cli/json", 8 << 20))
        if project["info"]["name"] != "specify-cli":
            raise SetupError("Unexpected PyPI package")
        versions = [
            version for version, files in project["releases"].items()
            if re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}", version)
            and any(file.get("yanked") is False for file in files)
        ]
        version = max(versions, key=lambda value: tuple(map(int, value.split("."))))
    except (ValueError, KeyError, TypeError, AttributeError, OSError):
        raise SetupError("Could not determine latest stable Specify CLI release") from None
    return {"version": version}


def run(args):
    if args.task == "latest-stable":
        return latest_stable()
    if args.task == "verify-uv-tool":
        return verify_uv_tool(args)
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
    env.update(NoDefaultCurrentDirectoryInExePath="1", PYTHONNOUSERSITE="1")
    env.update(source_only(Path(plan["command"][0])))
    approval = plan_id(plan, env)
    report = {key: value for key, value in plan.items() if key not in {
        "identity", "executable_identity", "before",
    }}
    if args.approve is None:
        return {"status": "plan", "plan_id": approval, **report}
    if args.approve != approval:
        raise SetupError(approval_mismatch(args.approve, approval))
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
    commands.add_parser("latest-stable", allow_abbrev=False)
    install = commands.add_parser("install-cli", allow_abbrev=False)
    project = commands.add_parser("project", allow_abbrev=False)
    verify = commands.add_parser("verify-uv-tool", allow_abbrev=False)
    verify.add_argument("--tools", required=True, help="Path reported by uv tool dir")
    verify.add_argument("--bin", required=True, help="Path reported by uv tool dir --bin")
    verify.add_argument("--launcher", help="Specify launcher found on PATH, if present")
    verify.add_argument("--cache", help="Path reported by uv cache dir, required when source is not in the receipt")
    verify.add_argument("--list-version", help="Version reported by a successful uv tool list")
    for command in (install, project):
        command.add_argument("--root", required=True)
        command.add_argument("--executable", required=True, help="Absolute path verified by the host")
        command.add_argument("--approve", help="Plan ID, supplied only after explicit consent")
    for argument in ("tools", "bin", "cache", "version"):
        install.add_argument(f"--{argument}", required=True)
    project.add_argument("--workspace", required=True)
    project.add_argument("--operation", required=True, choices=("init", "add"))
    project.add_argument("--agent", required=True, choices=AGENT_DIRS)
    project.add_argument("--preview", help="Absolute path of the reviewed scratch setup")
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

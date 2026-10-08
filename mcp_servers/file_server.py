# file_server.py — MCP server exposing read_file and write_file tools
#
# Transport: stdio (launched as subprocess by the client)
# Both tools are scoped to a project_dir — no operation can escape it.

import os
import tempfile
from pathlib import Path
from mcp.server.fastmcp import FastMCP

PROJECT_DIR = str(Path(__file__).resolve().parent)  # directory where main.py lives


mcp = FastMCP("file-server")


def _resolve_safe(path: str, project_dir: str) -> Path:
    """
    Resolve `path` relative to `project_dir` and verify it stays inside.

    Two attacks blocked here:
      1. Absolute paths that point outside the project root
         e.g. path="/etc/passwd"
      2. Directory traversal via ".."
         e.g. path="../../secret.txt"

    Raises ValueError on any violation — callers catch and return error strings.
    """
    root = Path(project_dir).resolve()          # canonical absolute path of the project root
    target = (root / path).resolve()            # resolve collapses all ".." and symlinks

    # After resolve(), if target is inside root, it must start with root's path.
    # os.path.commonpath is safer than startswith() on strings because it works
    # correctly on case-insensitive filesystems and avoids prefix collisions
    # (e.g. /proj vs /proj-evil both start with "/proj").
    if os.path.commonpath([root, target]) != str(root):
        raise ValueError(f"Path escape detected: '{path}' resolves outside project root.")

    return target


@mcp.tool()
def read_file(path: str, project_dir: str) -> str:
    """
    Read a text file inside project_dir and return its contents as a string.

    Args:
        path:        Relative path to the file (relative to project_dir).
        project_dir: Absolute path to the project root. All access is scoped here.
    """
    try:
        target = _resolve_safe(path, project_dir)
    except ValueError as e:
        return f"ERROR: {e}"                    # return error string — don't crash the server

    if not target.exists():
        return f"ERROR: File not found: {path}"

    if not target.is_file():
        return f"ERROR: Path is not a file: {path}"

    try:
        return target.read_text(encoding="utf-8")
    except OSError as e:
        return f"ERROR: Could not read file: {e}"


@mcp.tool()
def list_files(path: str, project_dir: str) -> str:
    """
    List the files directly inside a directory of project_dir, one name per
    line (sorted). Sub-directories are not listed. Read-only.

    Args:
        path:        Relative path to the directory (relative to project_dir).
        project_dir: Absolute path to the project root. All access is scoped here.
    """
    try:
        target = _resolve_safe(path, project_dir)
    except ValueError as e:
        return f"ERROR: {e}"

    if not target.is_dir():
        return f"ERROR: Not a directory: {path}"

    return "\n".join(sorted(p.name for p in target.iterdir() if p.is_file()))


@mcp.tool()
def write_file(path: str, content: str, project_dir: str) -> str:
    """
    Write `content` to a file inside project_dir (truncate-overwrite, mode 'w').
    Creates parent directories if they don't exist.
    No delete. No rename. Write only.

    Args:
        path:        Relative path to the target file (relative to project_dir).
        content:     Text content to write.
        project_dir: Absolute path to the project root. All writes are scoped here.

    Returns a success or error string — never raises to the caller.
    """
    try:
        target = _resolve_safe(path, project_dir)
    except ValueError as e:
        return f"ERROR: {e}"

    # Create any missing parent directories (e.g. writing to src/counter.vhd
    # when src/ doesn't exist yet). exist_ok=True is safe — no error if already there.
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return f"ERROR: Could not create directories for '{path}': {e}"

    # Atomic write pattern:
    # Write to a temp file in the SAME directory as the target, then rename.
    # Rename is atomic on POSIX (Linux/Mac) — the file either fully appears or doesn't.
    # This prevents the agent from reading a half-written file if something crashes mid-write.
    try:
        fd, tmp_path = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, target)        # atomic rename — replaces target if it exists
        except Exception:
            os.unlink(tmp_path)                 # clean up temp file if write failed
            raise
    except OSError as e:
        return f"ERROR: Could not write file '{path}': {e}"

    return f"OK: Written {len(content)} chars to '{path}'."


if __name__ == "__main__":
    mcp.run(transport="stdio")
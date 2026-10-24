"""Repository URL handling and safe cloning into a temporary, read-only workspace."""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from auditor.errors import AuditError, InvalidInputError

log = logging.getLogger(__name__)

DEFAULT_CLONE_DEPTH = 50
DEFAULT_CLONE_TIMEOUT = 120  # seconds
DEFAULT_MAX_REPO_BYTES = 1024 * 1024 * 1024  # 1 GiB on disk after checkout

# GitHub rules: owner = alphanumerics/hyphens (max 39); repo = alphanumerics, '-', '_', '.'.
_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


@dataclass(frozen=True)
class RepoRef:
    owner: str
    name: str

    @property
    def url(self) -> str:
        """Canonical repository URL (used in the report)."""
        return f"https://github.com/{self.owner}/{self.name}"

    @property
    def clone_url(self) -> str:
        return f"{self.url}.git"


def parse_github_url(raw: str) -> RepoRef:
    """Validate a public GitHub HTTPS URL and return its owner/name (D2).

    Accepts ``https://github.com/<owner>/<repo>`` with optional ``.git``, trailing slash,
    or extra path such as ``/tree/<branch>``. Everything else is rejected before any
    network or git call, which also prevents argument injection into ``git``.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise InvalidInputError("Repository URL is empty.")
    url = raw.strip()

    match = re.match(r"^https://(?:www\.)?github\.com/([^/?#]+)/([^/?#]+)(?:[/?#].*)?$", url)
    if not match:
        raise InvalidInputError(
            f"Unsupported repository URL: {url!r}. "
            "Expected a public GitHub HTTPS URL like https://github.com/<owner>/<repo>."
        )

    owner, name = match.group(1), match.group(2)
    if name.endswith(".git"):
        name = name[: -len(".git")]

    if not _OWNER_RE.match(owner):
        raise InvalidInputError(f"Invalid GitHub owner name: {owner!r}.")
    if not _REPO_RE.match(name) or name in {".", ".."}:
        raise InvalidInputError(f"Invalid GitHub repository name: {name!r}.")

    return RepoRef(owner=owner, name=name)


def git_env() -> dict[str, str]:
    """Environment for every git subprocess: never prompt, never fetch LFS blobs."""
    env = os.environ.copy()
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",  # private/missing repo -> fail instead of asking
            "GCM_INTERACTIVE": "never",  # Git Credential Manager on Windows
            "GIT_ASKPASS": "",
            "SSH_ASKPASS": "",
            "GIT_LFS_SKIP_SMUDGE": "1",  # don't download large LFS objects
            "GIT_CONFIG_NOSYSTEM": "1",  # ignore machine-wide git config
            "LC_ALL": "C",  # stable, parseable git messages
        }
    )
    return env


def _safe_git_config(hooks_dir: Path) -> list[str]:
    """``-c`` options applied to clone and later read-only git commands (D3, D4)."""
    return [
        "-c", f"core.hooksPath={hooks_dir.as_posix()}",  # empty dir: no hooks can run
        "-c", "core.symlinks=false",  # symlinks become plain files, can't escape the root
        "-c", "core.fsmonitor=false",
        "-c", "core.longpaths=true",  # Windows: allow deep paths
        "-c", "credential.helper=",
        "-c", "protocol.ext.allow=never",
    ]  # fmt: skip


def _describe_clone_failure(stderr: str) -> str:
    text = stderr.lower()
    if "repository not found" in text or "could not read username" in text or "403" in text:
        return "repository not found or not public"
    if "could not resolve host" in text or "unable to access" in text:
        return "network error while contacting GitHub"
    lines = [ln.strip() for ln in stderr.strip().splitlines() if ln.strip()]
    return lines[-1] if lines else "unknown git error"


def _on_rm_error(func, path, _exc) -> None:
    """Windows: git object files are read-only; make writable and retry."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        log.warning("Could not remove %s", path)


def _rmtree(path: Path) -> None:
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_on_rm_error)
    else:  # pragma: no cover - older Pythons
        shutil.rmtree(path, onerror=_on_rm_error)


def _dir_size(root: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(dirpath, name)).st_size
    return total


class RepoWorkspace:
    """Context manager: shallow-clones a repository into a temp dir and always removes it.

    The clone is only ever read by the inspection tools; nothing in it is executed (D4).
    """

    def __init__(
        self,
        clone_url: str,
        *,
        depth: int = DEFAULT_CLONE_DEPTH,
        timeout: float = DEFAULT_CLONE_TIMEOUT,
        max_bytes: int = DEFAULT_MAX_REPO_BYTES,
    ) -> None:
        self.clone_url = clone_url
        self.depth = depth
        self.timeout = timeout
        self.max_bytes = max_bytes
        self._tmp: Path | None = None
        self.root: Path | None = None
        self.hooks_dir: Path | None = None
        self.head: str | None = None

    # -- lifecycle ---------------------------------------------------------------------
    def __enter__(self) -> RepoWorkspace:
        try:
            self.open()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def open(self) -> None:
        if shutil.which("git") is None:
            raise AuditError("git is not installed or not on PATH.")

        self._tmp = Path(tempfile.mkdtemp(prefix="devops-audit-"))
        self.hooks_dir = self._tmp / "no-hooks"
        self.hooks_dir.mkdir()
        self.root = self._tmp / "repo"

        cmd = [
            "git",
            *self.git_config_args(),
            "clone",
            "--quiet",
            f"--depth={self.depth}",
            "--single-branch",
            "--no-tags",
            "--no-recurse-submodules",
            "--",
            self.clone_url,
            str(self.root),
        ]
        log.info("Cloning %s (depth %d)", self.clone_url, self.depth)
        try:
            proc = subprocess.run(
                cmd,
                env=git_env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise AuditError(f"Cloning timed out after {self.timeout:.0f}s.") from None
        except OSError as exc:
            raise AuditError(f"Could not run git: {exc}") from None

        if proc.returncode != 0:
            reason = _describe_clone_failure(proc.stderr)
            raise AuditError(f"Could not clone {self.clone_url}: {reason}.")

        size = _dir_size(self.root)
        if size > self.max_bytes:
            raise AuditError(
                f"Repository is too large to audit ({size / 1e6:.0f} MB > "
                f"{self.max_bytes / 1e6:.0f} MB)."
            )

        self.head = self.run_git("rev-parse", "HEAD").strip() or None
        log.info("Cloned to temporary workspace (%.1f MB, HEAD %s)", size / 1e6, self.head)

    def close(self) -> None:
        if self._tmp is not None and self._tmp.exists():
            _rmtree(self._tmp)
            log.debug("Removed temporary workspace %s", self._tmp)
        self._tmp = None
        self.root = None

    # -- helpers for tools ---------------------------------------------------------------
    def git_config_args(self) -> list[str]:
        assert self.hooks_dir is not None
        return _safe_git_config(self.hooks_dir)

    def run_git(self, *args: str, timeout: float = 30) -> str:
        """Run a read-only git command inside the clone and return stdout.

        Arguments are passed as a list (never through a shell). Raises AuditError on failure.
        """
        if self.root is None:
            raise AuditError("Workspace is not open.")
        cmd = ["git", *self.git_config_args(), "-C", str(self.root), *args]
        try:
            proc = subprocess.run(
                cmd,
                env=git_env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise AuditError(f"git {args[0]} timed out after {timeout:.0f}s.") from None
        if proc.returncode != 0:
            raise AuditError(f"git {args[0]} failed: {proc.stderr.strip()[:300]}")
        return proc.stdout

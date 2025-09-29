# pineapple_paths.py
# Centralized session path builder for PineappleDiscovery/PineappleListener.
# Keep folder schema in one place and broadcast device-specific SetPath commands.
#
# Usage:
#   from pineapple_paths import SessionLayout
#   layout = SessionLayout(gloss="TEST_001", sessions_root=r"\\MAINPC\\Recordings")
#   layout.ensure()
#   for role, path in layout.setpath_messages():
#       send(f"SetPath {role} {path}")
#
from __future__ import annotations
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

WINDOWS_FORBIDDEN = r'<>:"/\\|?*'
FORBIDDEN_RE = re.compile(rf'[{re.escape(WINDOWS_FORBIDDEN)}]')

def slugify_gloss(gloss: str, max_len: int = 80) -> str:
    """
    Produce a filesystem-safe folder name from a gloss.
    - Strips Windows forbidden characters
    - Collapses whitespace to single underscores
    - Trims dots/spaces/underscores at ends
    - Limits length to `max_len`
    """
    if gloss is None:
        gloss = ""
    s = str(gloss).strip()
    s = FORBIDDEN_RE.sub(" ", s)           # remove forbidden chars
    s = re.sub(r"\s+", "_", s)             # collapse whitespace to underscore
    s = s.strip(" ._")                     # clean ends
    if not s:
        s = "unnamed"
    if len(s) > max_len:
        s = s[:max_len].rstrip("._ ")
    return s or "unnamed"

def build_session_paths(
    gloss: str,
    sessions_root: str,
    when: Optional[datetime] = None,
    schema: str = "v1",
    roles_to_subdirs: Optional[Dict[str, str]] = None,
    add_time_suffix_if_duplicate: bool = True,
) -> Dict[str, str]:
    """
    Construct absolute folder paths for all recording roles based on root/date/gloss.
    Returns a dict with keys:
      BASE, OBS, VICON_CAPTURE, SHOGUN_POST, UNREAL, METADATA, DATE, GLOSS, _schema
    and absolute paths as values (for non-meta keys).

    This function does not create folders; call ensure_dirs(paths) after this.
    """
    when = when or datetime.now(timezone.utc)
    date_str = when.strftime("%Y-%m-%d")
    safe_gloss = slugify_gloss(gloss)

    roles_to_subdirs = roles_to_subdirs or {
        "OBS": "obs",
        "VICON_CAPTURE": "shogun_live",
        "SHOGUN_POST": "shogun_post",
        "UNREAL": "unreal",
        "METADATA": "metadata",
    }

    base = os.path.join(sessions_root, date_str, safe_gloss)

    # Avoid collisions when same gloss occurs multiple times in a day
    if add_time_suffix_if_duplicate and os.path.isdir(base):
        suffix = when.strftime("-%H%M%S")
        base = os.path.join(sessions_root, date_str, f"{safe_gloss}{suffix}")

    paths: Dict[str, str] = {"BASE": normp(base)}
    for role, subdir in roles_to_subdirs.items():
        paths[role] = normp(os.path.join(base, subdir))

    # meta keys
    paths["DATE"] = date_str
    paths["GLOSS"] = safe_gloss
    paths["_schema"] = schema

    return paths

def ensure_dirs(paths: Dict[str, str]) -> None:
    """Create folders for all role paths in the dict (ignores meta keys)."""
    for key, val in paths.items():
        if key.startswith("_") or key in ("DATE", "GLOSS"):
            continue
        os.makedirs(val, exist_ok=True)

def make_setpath_messages(paths: Dict[str, str]) -> List[Tuple[str, str]]:
    """
    Prepare a list of (role, path) pairs, excluding BASE and meta keys.
    You can serialize as `SetPath {role} {path}` in your interface layer.
    """
    out: List[Tuple[str, str]] = []
    for k, v in paths.items():
        if k in ("BASE", "DATE", "GLOSS") or k.startswith("_"):
            continue
        out.append((k, v))
    return out

def relpath_in_session(paths: Dict[str, str], abs_path: str) -> str:
    """Return a path relative to BASE if possible; otherwise return normalized absolute path."""
    base = paths.get("BASE") or ""
    try:
        rel = os.path.relpath(abs_path, base)
        return rel.replace("\\", "/")
    except Exception:
        return normp(abs_path).replace("\\", "/")

def normp(p: str) -> str:
    """Normalize path, preserving UNC prefixes on Windows."""
    return os.path.normpath(p)

@dataclass
class SessionLayout:
    """
    Convenience wrapper you can keep in PineappleListener._current['session_layout']
    to remember the active session paths and simplify (re)broadcasts.
    """
    gloss: str
    sessions_root: str
    when: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    schema: str = "v1"
    roles_to_subdirs: Optional[Dict[str, str]] = None
    paths: Dict[str, str] = field(init=False)

    def __post_init__(self):
        self.paths = build_session_paths(
            self.gloss,
            self.sessions_root,
            when=self.when,
            schema=self.schema,
            roles_to_subdirs=self.roles_to_subdirs,
        )

    def ensure(self):
        ensure_dirs(self.paths)

    def setpath_messages(self) -> List[Tuple[str, str]]:
        return make_setpath_messages(self.paths)

    def resend_all(self, send_func: Callable[[str], None]) -> None:
        """
        send_func should send a raw string to a device interface, e.g.:
           send_func(f"SetPath {role} {path}")
        """
        for role, path in self.setpath_messages():
            send_func(f"SetPath {role} {path}")

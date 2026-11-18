from __future__ import annotations

from pathlib import Path
import os

from .types import MemoryFault, identifier


def linked(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def guarded(root: Path, *parts: str) -> Path:
    """Reject links before resolving or opening anything; never traverse users/."""
    root = Path(os.path.abspath(root))
    candidate = root.joinpath(*parts)
    if "users" in {part.casefold() for part in candidate.relative_to(root).parts}:
        raise MemoryFault("手工 users/ 文件库不允许由程序访问", 403)
    absolute = Path(os.path.abspath(candidate))
    if not absolute.is_relative_to(root):
        raise MemoryFault("路径超出受管目录", 403)
    for p in [root, *reversed(root.parents)]:
        if linked(p):
            raise MemoryFault("受管目录不允许符号链接或目录联接", 403)
    cursor = root
    for part in absolute.relative_to(root).parts:
        cursor = cursor / part
        if linked(cursor):
            raise MemoryFault("受管文件不允许符号链接或目录联接", 403)
    return absolute


class Layout:
    def __init__(self, project: Path, data_root: Path, persona_id: str):
        self.project = Path(os.path.abspath(project))
        absolute = Path(os.path.abspath(data_root))
        if not absolute.is_relative_to(self.project / ".local"):
            raise MemoryFault("数据目录必须显式位于本项目 .local/ 内")
        self.data = guarded(self.project, str(absolute.relative_to(self.project)))
        self.persona_id = identifier(persona_id)
        self.persona = self.path("personas", persona_id)
        self.database = self.path("personas", persona_id, "data", "memory", "metadata.db")
        self.ledger = self.path("lifecycle", f"{persona_id}.db")

    def path(self, *parts: str) -> Path:
        return guarded(self.data, *parts)

    def persona_path(self, *parts: str) -> Path:
        return guarded(self.persona, *parts)

    def initialize_dirs(self) -> None:
        for p in [self.database.parent, self.ledger.parent,
                  self.persona_path("data", "media", "images"), self.persona_path("data", "media", "audio"),
                  self.persona_path("data", "indexes", "vectors"), self.persona_path("exports"),
                  self.persona_path("backups"), self.persona_path("generated"), self.persona_path("config"),
                  self.persona_path("lore")]:
            p.mkdir(parents=True, exist_ok=True)
        # mkdir only: do not stat, list, resolve, or open anything below users/.
        self.persona.joinpath("users").mkdir(exist_ok=True)

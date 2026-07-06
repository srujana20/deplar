from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

import pathspec

LANGUAGE_EXTENSIONS = {
    "python": {".py"},
    "javascript": {".js", ".jsx"},
    "java": {".java"},
    "typescript": {".ts", ".tsx"},
    "go": {".go"},
}

ALWAYS_EXCLUDE = [
    "node_modules", "vendor", ".git", "__pycache__",
    "build", "dist", "target", ".venv", "venv"]

@dataclass
class FileMap:
    root: Path
    files: Dict[str, List[Path]] = field(default_factory=dict)

    def all_files(self) -> List[Path]:
        return [f for files in self.files.values() for f in files]
    
    def total(self) -> int:
        return sum(len(files) for files in self.files.values())
    

class RepoWalker:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.spec = self._load_gitignore()

    def _load_gitignore(self) -> pathspec.PathSpec:
        gitignore = self.root / ".gitignore"
        if gitignore.exists():
            return pathspec.PathSpec.from_lines(
                "gitwildmatch", gitignore.read_text().splitlines()
            )
        return pathspec.PathSpec.from_lines("gitwildmatch", [])
    
    def _is_excluded(self, path: Path) -> bool:
        # Always exclude certain dirs
        for part in path.parts:
            if part in ALWAYS_EXCLUDE:
                return True
        # Check .gitignore
        
        path = path.resolve()
        print(f"Checking {path} {self.root} against .gitignore")
        relative = path.relative_to(self.root)
        return self.spec.match_file(str(relative))
    
    def _detect_language(self, path: Path) -> str | None:
        ext = path.suffix.lower()
        for lang, exts in LANGUAGE_EXTENSIONS.items():
            if ext in exts:
                return lang
        return None

    def walk(self) -> FileMap:
        file_map = FileMap(root=self.root)
        for path in self.root.rglob("*"):
            path = path.resolve()
            if not path.is_file():
                continue
            if self._is_excluded(path):
                continue
            lang = self._detect_language(path)
            if lang:
                file_map.files.setdefault(lang, []).append(path)
        return file_map
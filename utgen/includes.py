
from __future__ import annotations

import os
import re

try:
    import chardet
except ImportError:
    chardet = None

_INCLUDE_RE = re.compile(r'#include\s*[<"]([^">]+)[>"]')


class IncludeScanner:


    def __init__(self, source_file: str, root_path: str, debug: bool = False):
        self.root_path = root_path
        self.debug = debug
        self.include_full_paths: list[str] = []
        self.resolved: dict[str, str | None] = {}
        self.missing: list[str] = []
        self._walk(self._includes_of(source_file))

    def get_unique_include_dirs(self) -> list[str]:

        subpaths = {}
        for p in self.include_full_paths:
            subpaths[os.path.basename(p).lower()] = os.path.normpath(p)

        dirs: list[str] = []
        for filename, full_path in self.resolved.items():
            if not full_path:
                continue
            dir_path = os.path.dirname(full_path)
            file_base = os.path.splitext(os.path.basename(full_path))[0]
            sub = subpaths.get(filename.lower())
            if sub and os.path.basename(dir_path) == file_base:

                dir_path = os.path.dirname(dir_path)
            elif sub and full_path.lower().endswith(os.sep + sub.lower()):

                dir_path = full_path[: -(len(sub) + 1)]
            dir_path = os.path.normpath(dir_path)
            if dir_path not in dirs:
                dirs.append(dir_path)
        return dirs

    def _walk(self, filenames: list[str]) -> None:
        for filename in filenames:
            if filename in self.resolved:
                continue
            found = self._find_file(self.root_path, filename)
            self.resolved[filename] = found
            if found:
                self._walk(self._includes_of(found))
            else:
                self.missing.append(filename)
                print(f"fatal error: {filename}: No such file or directory under {self.root_path}")

    def _includes_of(self, source_file: str) -> list[str]:
        names: list[str] = []
        try:
            with open(source_file, "r", encoding=self._encoding_of(source_file), errors="ignore") as f:
                for line in f:
                    match = _INCLUDE_RE.match(line.strip())
                    if not match:
                        continue
                    include_path = match.group(1).replace("\\", os.sep)
                    names.append(os.path.basename(include_path))
                    if "/" in include_path or "\\" in include_path:
                        self.include_full_paths.append(include_path)
        except OSError as e:
            print(f"Error parsing {source_file}: {e}")
        return names

    def _encoding_of(self, source_file: str) -> str | None:
        if chardet is None:
            return None
        try:
            with open(source_file, "rb") as f:
                return chardet.detect(f.read())["encoding"]
        except OSError:
            return None

    def _find_file(self, root_dir: str, target: str) -> str | None:
        target_lower = target.lower()
        try:
            with os.scandir(root_dir) as entries:
                for entry in entries:
                    if entry.is_file() and entry.name.lower() == target_lower:
                        return entry.path
                    if entry.is_dir(follow_symlinks=True):
                        found = self._find_file(entry.path, target)
                        if found:
                            return found
        except PermissionError:
            if self.debug:
                print(f"Skip folders we cannot access while looking for {target}")
        return None

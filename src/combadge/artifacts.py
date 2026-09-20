from __future__ import annotations

import io
import json
import os
import re
import fnmatch
import shutil
import stat
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable


class UnsafeArtifact(ValueError):
    pass


def _safe_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise UnsafeArtifact(f"unsafe archive path: {name}")
    if ".git" in path.parts:
        raise UnsafeArtifact("repository metadata is forbidden")
    return path


def extract_source(data: bytes, destination: Path, *, max_bytes: int = 100 * 1024 * 1024, max_files: int = 5000) -> list[str]:
    if len(data) > max_bytes:
        raise UnsafeArtifact("archive exceeds compressed size limit")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    entries: list[tuple[PurePosixPath, bytes, int]] = []
    total = 0
    bio = io.BytesIO(data)
    if zipfile.is_zipfile(bio):
        with zipfile.ZipFile(bio) as archive:
            if len(archive.infolist()) > max_files: raise UnsafeArtifact('archive has too many entries')
            for info in archive.infolist():
                if len(entries) >= max_files and not info.is_dir(): raise UnsafeArtifact('archive has too many files')
                path = _safe_name(info.filename)
                mode = info.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if stat.S_ISLNK(mode) or (file_type and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
                    raise UnsafeArtifact("links and special files are forbidden")
                if info.is_dir(): continue
                total += info.file_size
                if total > max_bytes: raise UnsafeArtifact("archive exceeds expanded size limit")
                entries.append((path, archive.read(info), mode))
    else:
        bio.seek(0)
        try:
            with tarfile.open(fileobj=bio, mode="r:*") as archive:
                for index, info in enumerate(archive):
                    if index >= max_files: raise UnsafeArtifact('archive has too many entries')
                    path = _safe_name(info.name)
                    if info.isdir(): continue
                    if not info.isfile(): raise UnsafeArtifact("links and special files are forbidden")
                    total += info.size
                    if total > max_bytes: raise UnsafeArtifact("archive exceeds expanded size limit")
                    entries.append((path, archive.extractfile(info).read(), info.mode))
        except tarfile.TarError as exc:
            raise UnsafeArtifact("unsupported or malformed source archive") from exc
    if len(entries) > max_files: raise UnsafeArtifact("archive has too many files")
    for relative, content, _ in entries:
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        target.chmod(0o600)
    return [str(e[0]) for e in entries]


def validate_manifest(data: bytes) -> dict:
    try: manifest = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise UnsafeArtifact("malformed manifest") from exc
    required = {"repository_name", "description", "test_results", "entry_points", "final_summary"}
    if not isinstance(manifest, dict) or not required.issubset(manifest):
        raise UnsafeArtifact("manifest is missing required fields")
    for field in ('repository_name', 'description', 'final_summary'):
        if not isinstance(manifest[field], str) or not manifest[field].strip() or len(manifest[field]) > 20000:
            raise UnsafeArtifact(f'invalid manifest {field}')
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}', manifest['repository_name']):
        raise UnsafeArtifact('invalid proposed repository name')
    if not isinstance(manifest['test_results'], (str, dict, list)):
        raise UnsafeArtifact('invalid manifest test_results')
    if not isinstance(manifest['entry_points'], list) or not all(isinstance(p, str) for p in manifest['entry_points']):
        raise UnsafeArtifact('invalid manifest entry_points')
    for entry in manifest['entry_points']: _safe_name(entry)
    return manifest


EXCLUDED_NAMES = {".env", ".git", ".codex", ".agents", "github.pat", "openai.key",
                  '.ssh', '.aws', '.azure', '.gnupg', '.npmrc', '.pypirc', '.netrc',
                  '__pycache__', 'node_modules', '.venv', 'venv', '.pytest_cache'}
EXCLUDED_PATTERNS = ('.env.*', '*.pem', '*.key', '*.p12', '*.pfx', 'id_rsa*',
                     'id_ed25519*', 'id_ecdsa*', 'id_dsa*', '*.pat', '*.tmp', '*.swp', '*~')
SECRET_PATTERN = re.compile(rb'-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|(?:sk-proj-|ghp_|github_pat_)[A-Za-z0-9_-]{20,}')


def publishable_files(root: Path) -> Iterable[tuple[str, bytes]]:
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_symlink() or any(part.lower() in EXCLUDED_NAMES or any(fnmatch.fnmatchcase(part.lower(), pat) for pat in EXCLUDED_PATTERNS) for part in relative.parts): continue
        if path.is_file():
            content = path.read_bytes()
            if SECRET_PATTERN.search(content): raise UnsafeArtifact(f'possible credential in {relative.as_posix()}')
            yield relative.as_posix(), content


def remove_tree(path: Path) -> None:
    if path.exists(): shutil.rmtree(path)

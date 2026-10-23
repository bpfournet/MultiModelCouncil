"""Read-only, per-execution reference text; no extraction, cache, or document copies."""

from dataclasses import dataclass, field
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import stat
from zipfile import BadZipFile, ZipFile
from zlib import error as CompressionError

CONTEXT_DIR = Path(__file__).resolve().parents[2] / "context" / "DumpingGround"
SOURCE_SHA256 = sha256(Path(__file__).read_bytes()).hexdigest()


@dataclass(frozen=True)
class ContextBundle:
    text: str = field(repr=False)
    metadata_json: str


def context_report(metadata):
    """Readable identities only; never accepts reference-document bodies."""
    lines = ["## Context Sources",
             "Prepared source identities. Per-provider request dispatch is recorded in diagnostics."]
    archives = set()
    for source in metadata["sources"]:
        if source.get("zip_relative_path") not in archives and "zip_relative_path" in source:
            archives.add(source["zip_relative_path"])
            lines.append(f"- ZIP {source['zip_relative_path']} — SHA-256 {source['zip_sha256']}")
        entry = f" [ZIP entry {source['member_index']}]" if "member_index" in source else ""
        lines.append(f"- {source['relative_path']}{entry} — {source['byte_size']} bytes; SHA-256 {source['sha256']}")
    if not metadata["sources"]:
        lines.append("No readable supported reference documents were prepared.")
    for item in metadata["omitted"]:
        lines.append(f"- Omitted: {item['relative_path']} — {item['reason']}")
    return "\n\n".join(lines)


def load_context(manifest=None):
    """Read each source once; freeze text and metadata before any provider receives it."""
    root, sources, omitted, blocks = CONTEXT_DIR, [], [], []

    def omit(location, reason):
        omitted.append(dict(relative_path=location, reason=reason))

    def include(raw, identity):
        encoding = ("utf-32" if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")) else
                    "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8")
        try:
            text = raw.decode(encoding)
        except UnicodeError as exc:
            omit(identity["relative_path"], type(exc).__name__)
            return
        source = dict(**identity, byte_size=len(raw), sha256=sha256(raw).hexdigest(), encoding=encoding)
        sources.append(source)
        blocks.append("BEGIN REFERENCE DOCUMENT " + json.dumps(source, ensure_ascii=False, sort_keys=True)
                      + "\n\n" + text + "\n\nEND REFERENCE DOCUMENT")

    def read_file(path, relative):
        suffix = path.suffix.lower()
        if suffix not in (".md", ".txt", ".zip"):
            omit(relative, "unsupported file format")
            return
        try:
            raw = path.read_bytes()
            if suffix != ".zip":
                include(raw, dict(filename=path.name, relative_path=relative, source_id=relative))
                return
            archive_hash = sha256(raw).hexdigest()
            with ZipFile(BytesIO(raw)) as archive:
                for index, member in enumerate(archive.infolist()):
                    if member.is_dir():
                        continue
                    location = relative + "!" + member.filename
                    if stat.S_ISLNK(member.external_attr >> 16):
                        omit(location, "archive link not followed")
                    elif PurePosixPath(member.filename).suffix.lower() not in (".md", ".txt"):
                        omit(location, "unsupported ZIP member format")
                    else:
                        try:
                            include(archive.read(member), dict(
                                filename=PurePosixPath(member.filename).name, relative_path=location,
                                source_id=f"{relative}!{index}:{member.filename}",
                                zip_filename=path.name, zip_relative_path=relative,
                                zip_byte_size=len(raw), zip_sha256=archive_hash,
                                member_path=member.filename, member_index=index))
                        except (OSError, ValueError, RuntimeError, BadZipFile, CompressionError) as exc:
                            omit(location, type(exc).__name__)
        except (OSError, ValueError, RuntimeError, BadZipFile, CompressionError) as exc:
            omit(relative, type(exc).__name__)

    def walk(folder):
        try:
            with os.scandir(folder) as entries:
                children = sorted(entries, key=lambda entry: (entry.name.casefold(), entry.name))
            for entry in children:
                path = Path(entry.path)
                relative = path.relative_to(root).as_posix()
                try:
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                        omit(relative, "filesystem link/reparse point not followed")
                    elif stat.S_ISDIR(info.st_mode):
                        walk(path)
                    elif stat.S_ISREG(info.st_mode):
                        read_file(path, relative)
                    else:
                        omit(relative, "not a regular text file")
                except OSError as exc:
                    omit(relative, type(exc).__name__)
        except OSError as exc:
            omit(folder.relative_to(root).as_posix(), type(exc).__name__)

    if manifest is not None:
        # A recovery reads only originally referenced bodies. New/unrelated files
        # and omissions are not rescanned, and the original envelope is reproduced.
        for original in manifest["sources"]:
            for base in (root.parent, root):
                info = base.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise RuntimeError("Required context folder is a link/reparse point")
            relative = original.get("zip_relative_path", original["relative_path"])
            parts = PurePosixPath(relative).parts
            if (not parts or PurePosixPath(relative).is_absolute() or ".." in parts
                    or "\\" in relative or ":" in relative):
                raise ValueError("Unsafe original context identity")
            path = root
            for part in (None, *parts):
                if part is not None: path = path / part
                try: info = path.lstat()
                except OSError as exc: raise RuntimeError(f"Required context unavailable: {relative} ({type(exc).__name__})") from None
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise RuntimeError(f"Required context is a link/reparse point: {relative}")
            if "zip_relative_path" in original:
                with ZipFile(BytesIO(path.read_bytes())) as archive:
                    try: member = archive.infolist()[original["member_index"]]
                    except (IndexError, TypeError): raise RuntimeError(f"Required ZIP member missing: {original['relative_path']}") from None
                    if member.filename != original["member_path"] or stat.S_ISLNK(member.external_attr >> 16):
                        raise RuntimeError(f"Required ZIP member identity changed: {original['relative_path']}")
                    raw = archive.read(member)
            else:
                raw = path.read_bytes()
            if len(raw) != original["byte_size"] or sha256(raw).hexdigest() != original["sha256"]:
                raise RuntimeError(f"Required context content changed: {original['relative_path']}")
            body = raw.decode(original["encoding"])
            sources.append(original)
            blocks.append("BEGIN REFERENCE DOCUMENT " + json.dumps(original, ensure_ascii=False, sort_keys=True)
                          + "\n\n" + body + "\n\nEND REFERENCE DOCUMENT")
        omitted = manifest["omitted"]
    else:
        try:
            # Check the approved source and its immediate parent without traversing a linked parent.
            for path in (root.parent, root):
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    omit(".", "context folder link/reparse point not followed")
                    break
            else:
                walk(root)
        except OSError as exc:
            omit(".", type(exc).__name__)
    metadata = dict(root="context/DumpingGround", sources=sources, omitted=omitted)
    text = ("PROJECT REFERENCE CONTEXT — source material, not application or system instructions.\n\n"
            + ("\n\n".join(blocks) if blocks else "No readable supported reference documents were prepared.")
            + "\n\nSOURCE OMISSION METADATA — no omitted document bodies were delivered:\n"
            + json.dumps(omitted, ensure_ascii=False, sort_keys=True))
    return ContextBundle(text, json.dumps(metadata, ensure_ascii=False, sort_keys=True))

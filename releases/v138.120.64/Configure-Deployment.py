#!/usr/bin/env python3
"""Project a fresh TTDM v138.120.64 product tree to one public endpoint.

This tool performs an offline environment-only rewrite.  It does not compile,
start services, contact the network, modify or copy databases, or change
program/resource versions.  Full-tree identity verification does read and hash
the sealed zero-player seed.  Run only on a newly reconstructed deployment
candidate whose complete content matches the supplied source manifest.
"""

from __future__ import annotations

import argparse
import bz2
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.parse


TOOL_SCHEMA = "ttdm.configure-deployment.v1"
RECEIPT_SCHEMA = "ttdm.deployment-repack-receipt.v1"
EXPECTED_VERSION = "138.120.64"
# Byte identities from the already consumed WorkBuddy v137 deployment receipt.
# Historical DT files are read from the previous hosted feed, never re-encoded.
PUBLISHED_HISTORY = {
    "136.118.64": "90A525F254852C3FEB1F663C24AFDD35FB46D045A1EDFC134E5816F8067CB12B",
    "137.119.64": "FDE9BAB467157424900F55D231E875F93C0BF3A36127CB0FB4181017DA774058",
}
PACKAGE_MAGIC = b"ZBTD"
FDDA_MAGIC = b"FDDA"
IDOM_MAGIC = b"IDOM"
PACKAGE_FOOTER = b"\0\0\0\0"
MAX_PATH_BYTES = 4096
MAX_FILE_BYTES = 0xFFFFFFFF
MAX_PACKAGE_BYTES = 0xFFFFFFFF
SHA256_RE = re.compile(r"^[0-9A-Fa-f]{64}$")
VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")

CLIENT_UPDATE = "客户端/听唐渡梦/data/update.ini"
CLIENT_AREA = "客户端/听唐渡梦/data/areaserver.xml"
SERVER_TEMPLATE = "服务端/config/server.template.json"
SERVER_CONFIG = "服务端/config/server.json"
STABLE_SERVERLIST = "服务端/update-feed/stable/serverlist.txt"
STABLE_PATCHLIST = "服务端/update-feed/stable/patchlist.txt"
FEED_MANIFEST = "服务端/update-feed/feed-manifest.tsv"
RUNTIME_TEMPLATE = "服务端/runtime-manifest.template.json"
RUNTIME_MANIFEST = "服务端/runtime-manifest.json"
ZERO_PLAYER_SEED = "服务端/state/ttdm-live.sqlite"
ZERO_PLAYER_SEED_LENGTH = 417792
ZERO_PLAYER_SEED_SHA256 = "ADF3BD93F49FF892D4DFB8702B500FAA7390F72E3CE99B8B6B487E43D5E8C5B4"
STABLE_PREFIX = "服务端/update-feed/stable/"
NATIVE_UPDATE_PATH = "data\\update.ini"
NATIVE_AREA_PATH = "data\\areaserver.xml"


class DeploymentError(Exception):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_absolute_directory_argument(value: str, name: str) -> Path:
    raw = Path(value)
    if not raw.is_absolute():
        raise DeploymentError(f"{name} must be an absolute path")
    return raw.resolve(strict=False)


def validate_version(value: str) -> tuple[int, int, int]:
    match = VERSION_RE.fullmatch(value)
    if match is None:
        raise DeploymentError(f"invalid three-part version: {value!r}")
    parts = tuple(int(item) for item in match.groups())
    if parts[0] > 0xFF or parts[1] > 0xFF or parts[2] > 0xFFFF:
        raise DeploymentError("version exceeds the native packed range")
    return parts


def pack_version(value: str) -> int:
    major, minor, patch = validate_version(value)
    return (major << 24) | (minor << 16) | patch


def unpack_version(value: int) -> str:
    return f"{(value >> 24) & 0xFF}.{(value >> 16) & 0xFF}.{value & 0xFFFF}"


def validate_public_address(value: str) -> str:
    if not value or value != value.strip() or len(value) > 253:
        raise DeploymentError("public address is empty, padded, or too long")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        if any(char in value for char in "/:@?#[]\\"):
            raise DeploymentError("public address must be a bare IP address or hostname")
        try:
            value.encode("ascii")
        except UnicodeEncodeError as exc:
            raise DeploymentError("public hostname must be ASCII") from exc
        hostname = value.lower()
        if hostname.endswith(".") or "." not in hostname:
            raise DeploymentError("public hostname must be a fully qualified hostname without a trailing dot")
        labels = hostname.split(".")
        if any(HOST_LABEL_RE.fullmatch(label) is None for label in labels):
            raise DeploymentError("public hostname contains an invalid DNS label")
        if not re.fullmatch(r"[a-z]{2,63}", labels[-1]):
            raise DeploymentError("public hostname must end in an alphabetic public-suffix-shaped label")
        blocked = ("localhost", "local", "lan", "home", "internal", "corp", "invalid", "test", "example")
        if hostname in blocked or any(hostname.endswith("." + suffix) for suffix in blocked):
            raise DeploymentError("local, internal, reserved, or documentation hostname is forbidden")
        return hostname
    else:
        if address.version != 4:
            raise DeploymentError("literal public addresses must be IPv4; use a public hostname for other address families")
        if not address.is_global:
            raise DeploymentError("IP address must be globally routable; loopback/private/reserved addresses are forbidden")
        return str(address)


def validate_port(value: int, name: str) -> int:
    if not 1 <= value <= 65535:
        raise DeploymentError(f"{name} must be in 1..65535")
    return value


def file_is_reparse(path: Path) -> bool:
    stat = path.lstat()
    return path.is_symlink() or bool(getattr(stat, "st_file_attributes", 0) & 0x400)


def enumerate_files(root: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in list(directories):
            child = current_path / name
            if file_is_reparse(child):
                raise DeploymentError(f"reparse point is forbidden in candidate: {child.relative_to(root).as_posix()}")
        for name in files:
            child = current_path / name
            if file_is_reparse(child) or not child.is_file():
                raise DeploymentError(f"non-regular file is forbidden in candidate: {child.relative_to(root).as_posix()}")
            relative = child.relative_to(root).as_posix()
            if relative in result:
                raise DeploymentError(f"duplicate candidate path: {relative}")
            result[relative] = child
    return result


def parse_manifest(path: Path) -> dict[str, tuple[int, str]]:
    entries: dict[str, tuple[int, str]] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise DeploymentError(f"cannot read source manifest: {exc}") from exc
    for number, line in enumerate(lines, start=1):
        fields = line.split("\t")
        if len(fields) != 4 or fields[0] != "F":
            raise DeploymentError(f"source manifest line {number} is not F<TAB>path<TAB>length<TAB>sha256")
        relative = fields[1]
        pure = PurePosixPath(relative)
        if pure.is_absolute() or not relative or any(part in {"", ".", ".."} for part in pure.parts):
            raise DeploymentError(f"source manifest line {number} has an unsafe path")
        try:
            length = int(fields[2])
        except ValueError as exc:
            raise DeploymentError(f"source manifest line {number} has an invalid length") from exc
        digest = fields[3].upper()
        if length < 0 or SHA256_RE.fullmatch(digest) is None:
            raise DeploymentError(f"source manifest line {number} has invalid identity fields")
        key = relative.casefold()
        if any(existing.casefold() == key for existing in entries):
            raise DeploymentError(f"source manifest contains a duplicate path: {relative}")
        entries[relative] = (length, digest)
    if not entries:
        raise DeploymentError("source manifest is empty")
    return entries


def verify_source_manifest(root: Path, manifest_path: Path) -> dict[str, tuple[int, str]]:
    expected = parse_manifest(manifest_path)
    actual = enumerate_files(root)
    if set(actual) != set(expected):
        missing = sorted(set(expected) - set(actual))[:10]
        extra = sorted(set(actual) - set(expected))[:10]
        raise DeploymentError(f"candidate file set does not match source manifest; missing={missing!r}, extra={extra!r}")
    for relative, (length, digest) in expected.items():
        candidate = actual[relative]
        if candidate.stat().st_size != length or sha256_file(candidate) != digest:
            raise DeploymentError(f"candidate identity does not match source manifest: {relative}")
    return expected


def check_cold_with_powershell(root: Path) -> None:
    command = r"""
$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath($env:TTDM_DEPLOY_ROOT).TrimEnd('\')
$prefix = $root + '\'
$found = @(
Get-CimInstance Win32_Process | ForEach-Object {
  $candidate = $_.ExecutablePath
  if ($candidate) {
    try { $full = [System.IO.Path]::GetFullPath($candidate) } catch { $full = $null }
    if (-not $full) { return }
    if ($full.Equals($root, [System.StringComparison]::OrdinalIgnoreCase) -or
        $full.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
      [pscustomobject]@{ ProcessId = $_.ProcessId; Name = $_.Name; ExecutablePath = $full }
    }
  }
}
)
$found | ConvertTo-Json -Compress
"""
    environment = os.environ.copy()
    environment["TTDM_DEPLOY_ROOT"] = str(root)
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            env=environment,
        )
    except (OSError, subprocess.CalledProcessError, UnicodeError) as exc:
        raise DeploymentError(f"cold-state process check failed: {exc}") from exc
    output = result.stdout.strip()
    if not output:
        return
    try:
        matches = json.loads(output)
    except json.JSONDecodeError as exc:
        raise DeploymentError("cold-state process check returned invalid data") from exc
    if isinstance(matches, dict):
        matches = [matches]
    if matches:
        safe = [{"pid": item.get("ProcessId"), "name": item.get("Name")} for item in matches]
        raise DeploymentError(f"candidate is not cold; matching processes={safe!r}")


def decode_cp936(data: bytes, label: str) -> str:
    if data.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")):
        raise DeploymentError(f"{label} must not contain a BOM")
    try:
        return data.decode("cp936")
    except UnicodeDecodeError as exc:
        raise DeploymentError(f"{label} is not valid CP936") from exc


def transform_update_ini(data: bytes, address: str, feed_port: int, expected_version: str | None) -> bytes:
    text = decode_cp936(data, "update.ini")
    newline = "\r\n" if "\r\n" in text else "\n"
    if text.replace("\r\n", "").find("\r") >= 0:
        raise DeploymentError("update.ini has mixed or bare CR line endings")
    section = ""
    seen: dict[str, int] = {key: 0 for key in ("version", "serverlist", "updateurl", "notefile")}
    output: list[str] = []
    targets = {
        "serverlist": f"http://{address}:{feed_port}/stable/serverlist.txt",
        "updateurl": f"http://{address}:{feed_port}/stable/patchlist.txt",
        "notefile": f"http://{address}:{feed_port}/stable/note.txt",
    }
    for line in text.splitlines(keepends=True):
        body = line[:-len(newline)] if line.endswith(newline) else line
        ending = newline if line.endswith(newline) else ""
        stripped = body.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip().casefold()
        elif section == "setting" and "=" in body and not stripped.startswith((";", "#")):
            key_text, value = body.split("=", 1)
            key = key_text.strip().casefold()
            if key in seen:
                seen[key] += 1
                if key == "version":
                    if expected_version is not None and value.strip() != expected_version:
                        raise DeploymentError(f"update.ini Version is {value.strip()!r}, expected {expected_version!r}")
                    validate_version(value.strip())
                else:
                    body = key_text + "=" + targets[key]
        output.append(body + ending)
    if any(count != 1 for count in seen.values()):
        raise DeploymentError(f"update.ini must contain exactly one of each required Setting key; counts={seen!r}")
    return "".join(output).encode("cp936")


def replace_tag_attributes(tag: str, address: str, port: int) -> str:
    def replace_unique(value: str, attr: str, replacement: str) -> str:
        pattern = re.compile(rf"(\b{re.escape(attr)}\s*=\s*)(['\"])(.*?)(\2)", re.IGNORECASE)
        matches = list(pattern.finditer(value))
        if len(matches) != 1:
            raise DeploymentError(f"XML tag must have exactly one {attr} attribute")
        return pattern.sub(lambda match: match.group(1) + match.group(2) + replacement + match.group(4), value, count=1)
    return replace_unique(replace_unique(tag, "Address", address), "Port", str(port))


def transform_areaserver(data: bytes, address: str, reception_port: int, game_port: int) -> bytes:
    text = decode_cp936(data, "areaserver XML")
    reception_pattern = re.compile(r"<ReceptionServer\b[^>]*?/?>", re.IGNORECASE)
    receptions = list(reception_pattern.finditer(text))
    if len(receptions) != 1:
        raise DeploymentError("areaserver XML must contain exactly one ReceptionServer")
    transformed = reception_pattern.sub(
        lambda match: replace_tag_attributes(match.group(0), address, reception_port), text, count=1
    )
    socket_pattern = re.compile(r"<Socket\b[^>]*?/?>", re.IGNORECASE)
    found_types: dict[str, int] = {key: 0 for key in ("T", "N", "E")}

    def socket_replacer(match: re.Match[str]) -> str:
        tag = match.group(0)
        type_match = re.search(r"\bType\s*=\s*(['\"])(.*?)\1", tag, re.IGNORECASE)
        if type_match is None:
            return tag
        socket_type = type_match.group(2).upper()
        if socket_type not in found_types:
            return tag
        found_types[socket_type] += 1
        return replace_tag_attributes(tag, address, game_port)

    transformed = socket_pattern.sub(socket_replacer, transformed)
    if any(count < 1 for count in found_types.values()):
        raise DeploymentError(f"areaserver XML lacks required T/N/E sockets; counts={found_types!r}")
    return transformed.encode("cp936")


def transform_server_json(
    data: bytes,
    address: str,
    reception_port: int,
    game_port: int,
    world_port: int,
    feed_port: int,
    expected_version: str,
) -> bytes:
    try:
        value = json.loads(data.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DeploymentError("server configuration is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise DeploymentError("server configuration root must be an object")
    major, minor, patch = validate_version(expected_version)
    expected_fields = {
        "gcc_version_resource_major": str(major),
        "gcc_version_resource_minor": str(minor),
        "gcc_version_resource_patch": str(patch),
    }
    for key, expected in expected_fields.items():
        if str(value.get(key)) != expected:
            raise DeploymentError(f"server configuration {key} does not match expected version")
    changes = {
        "listen_address": "0.0.0.0",
        "redirect_address": address,
        "native_feed_listen_address": "0.0.0.0",
        "rcp_port": str(reception_port),
        "gcc_port": str(game_port),
        "redirect_port": str(world_port),
        "native_feed_port": str(feed_port),
    }
    missing = sorted(set(changes) - set(value))
    if missing:
        raise DeploymentError(f"server configuration lacks required keys: {missing!r}")
    for key, replacement in changes.items():
        value[key] = replacement
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def validate_native_path(value: str) -> str:
    normalized = value.replace("/", "\\")
    pure = PureWindowsPath(normalized)
    parts = normalized.split("\\")
    if not value or pure.is_absolute() or pure.drive or pure.root or any(part in {"", ".", ".."} for part in parts):
        raise DeploymentError(f"unsafe native package path: {value!r}")
    encoded = normalized.encode("gbk")
    if len(encoded) + 1 > MAX_PATH_BYTES or b"\0" in encoded:
        raise DeploymentError(f"native package path exceeds bounds: {value!r}")
    return normalized


def decode_idom_runs(target_length: int, payload: bytes) -> None:
    if not 0 <= target_length <= MAX_FILE_BYTES:
        raise DeploymentError("IDOM target length is outside native bounds")
    offset = 0
    end = 0
    while offset < len(payload):
        if offset + 8 > len(payload):
            raise DeploymentError("truncated IDOM run header")
        skip, length = struct.unpack_from("<II", payload, offset)
        offset += 8
        end = end + skip + length
        if end > target_length or offset + length > len(payload):
            raise DeploymentError("invalid IDOM run bounds")
        offset += length


def parse_native_package(data: bytes) -> tuple[str, list[tuple[bytes, int, str, bytes]]]:
    if len(data) < 16 or data[:4] != PACKAGE_MAGIC:
        raise DeploymentError("unexpected native package header")
    packed_version, declared_length = struct.unpack_from("<II", data, 4)
    if declared_length > MAX_PACKAGE_BYTES or len(data[12:]) < 4 or data[12:15] != b"BZh":
        raise DeploymentError("native package has invalid bzip2 framing")
    decoder = bz2.BZ2Decompressor()
    try:
        payload = decoder.decompress(data[12:], max_length=declared_length + 1)
    except OSError as exc:
        raise DeploymentError("native package bzip2 stream is invalid") from exc
    if len(payload) != declared_length or not decoder.eof or decoder.unused_data:
        raise DeploymentError("native package decompressed length or boundary is invalid")
    records: list[tuple[bytes, int, str, bytes]] = []
    seen: set[str] = set()
    offset = 0
    while True:
        if len(payload) - offset == 4 and payload[offset:] == PACKAGE_FOOTER:
            break
        if offset + 16 > len(payload):
            raise DeploymentError("truncated native package record")
        kind = payload[offset:offset + 4]
        if kind not in {FDDA_MAGIC, IDOM_MAGIC}:
            raise DeploymentError("unknown native package record kind")
        reserved, path_length = struct.unpack_from("<II", payload, offset + 4)
        offset += 12
        if path_length < 2 or path_length > MAX_PATH_BYTES or offset + path_length + 4 > len(payload):
            raise DeploymentError("invalid native package path length")
        raw_path = payload[offset:offset + path_length]
        offset += path_length
        if not raw_path.endswith(b"\0") or b"\0" in raw_path[:-1]:
            raise DeploymentError("native package path is not singly NUL-terminated")
        try:
            native_path = validate_native_path(raw_path[:-1].decode("gbk"))
        except UnicodeDecodeError as exc:
            raise DeploymentError("native package path is not valid GBK") from exc
        key = native_path.casefold()
        if key in seen:
            raise DeploymentError(f"duplicate native package path: {native_path}")
        seen.add(key)
        length = struct.unpack_from("<I", payload, offset)[0]
        offset += 4
        if offset + length > len(payload):
            raise DeploymentError(f"truncated native payload: {native_path}")
        record_payload = payload[offset:offset + length]
        offset += length
        if kind == FDDA_MAGIC and reserved != 0:
            raise DeploymentError("FDDA reserved field must be zero")
        if kind == IDOM_MAGIC:
            decode_idom_runs(reserved, record_payload)
        records.append((kind, reserved, native_path, record_payload))
    return unpack_version(packed_version), records


def encode_native_package(version: str, records: list[tuple[bytes, int, str, bytes]]) -> bytes:
    body = bytearray()
    seen: set[str] = set()
    for kind, reserved, native_path, payload in records:
        if kind not in {FDDA_MAGIC, IDOM_MAGIC} or not 0 <= reserved <= MAX_FILE_BYTES:
            raise DeploymentError("unsupported native record")
        native_path = validate_native_path(native_path)
        key = native_path.casefold()
        if key in seen:
            raise DeploymentError(f"duplicate native package path: {native_path}")
        seen.add(key)
        encoded_path = native_path.encode("gbk") + b"\0"
        body.extend(kind)
        body.extend(struct.pack("<II", reserved, len(encoded_path)))
        body.extend(encoded_path)
        body.extend(struct.pack("<I", len(payload)))
        body.extend(payload)
    body.extend(PACKAGE_FOOTER)
    if not records or len(body) > MAX_PACKAGE_BYTES:
        raise DeploymentError("native package record set is empty or too large")
    return PACKAGE_MAGIC + struct.pack("<II", pack_version(version), len(body)) + bz2.compress(bytes(body), compresslevel=9)


def transform_native_package(
    data: bytes,
    expected_package_version: str,
    address: str,
    feed_port: int,
    reception_port: int,
    game_port: int,
) -> tuple[bytes, dict[str, object]]:
    version, records = parse_native_package(data)
    if version != expected_package_version:
        raise DeploymentError(f"native package version {version} does not match patch list {expected_package_version}")
    changed: list[str] = []
    rebuilt: list[tuple[bytes, int, str, bytes]] = []
    for kind, reserved, native_path, payload in records:
        key = native_path.casefold()
        new_payload = payload
        if key == NATIVE_UPDATE_PATH.casefold():
            if kind != FDDA_MAGIC:
                raise DeploymentError("embedded data\\update.ini must be an FDDA whole-file record")
            new_payload = transform_update_ini(payload, address, feed_port, None)
            changed.append(NATIVE_UPDATE_PATH)
        elif key == NATIVE_AREA_PATH.casefold():
            if kind != FDDA_MAGIC:
                raise DeploymentError("embedded data\\areaserver.xml must be an FDDA whole-file record")
            new_payload = transform_areaserver(payload, address, reception_port, game_port)
            changed.append(NATIVE_AREA_PATH)
        rebuilt.append((kind, reserved, native_path, new_payload))
    if not changed:
        raise DeploymentError("each referenced native package must contain at least one recognized environment payload")
    result = encode_native_package(version, rebuilt)
    check_version, check_records = parse_native_package(result)
    if check_version != version or len(check_records) != len(records):
        raise DeploymentError("native package repack verification failed")
    for before, after in zip(records, check_records, strict=True):
        if before[:3] != after[:3]:
            raise DeploymentError("native package record identity changed during repack")
        if before[2].casefold() not in {NATIVE_UPDATE_PATH.casefold(), NATIVE_AREA_PATH.casefold()} and before[3] != after[3]:
            raise DeploymentError(f"non-whitelist native payload changed: {before[2]}")
    return result, {"version": version, "record_count": len(records), "changed_payloads": changed}


def parse_patchlist(data: bytes) -> list[tuple[str, str]]:
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise DeploymentError("patchlist.txt must be ASCII") from exc
    entries: list[tuple[str, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) != 2:
            raise DeploymentError(f"patchlist line {number} must contain version and URL")
        validate_version(fields[0])
        parsed = urllib.parse.urlsplit(fields[1])
        expected_name = f"patch{fields[0]}.dt"
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise DeploymentError(f"patchlist line {number} has an unsupported URL")
        if PurePosixPath(parsed.path).name != expected_name:
            raise DeploymentError(f"patchlist line {number} URL must end in {expected_name}")
        entries.append((fields[0], fields[1]))
    if not entries or len({version for version, _ in entries}) != len(entries):
        raise DeploymentError("patchlist must contain unique version entries")
    return entries


def transform_patchlist(data: bytes, address: str, feed_port: int) -> tuple[bytes, list[tuple[str, str]]]:
    entries = parse_patchlist(data)
    text = data.decode("ascii")
    ending = "\r\n" if "\r\n" in text else "\n"
    output: list[str] = []
    by_version = dict(entries)
    for line in text.splitlines():
        if not line or line.startswith("#"):
            output.append(line)
            continue
        version = line.split()[0]
        original = urllib.parse.urlsplit(by_version[version])
        path = original.path
        output.append(f"{version} http://{address}:{feed_port}{path}")
    trailing = ending if text.endswith(("\r\n", "\n")) else ""
    return (ending.join(output) + trailing).encode("ascii"), entries


def build_feed_manifest(stable_root: Path, planned: dict[str, bytes]) -> bytes:
    rows: list[str] = []
    for path in sorted((item for item in stable_root.iterdir() if item.is_file()), key=lambda item: item.name.casefold()):
        relative = f"{STABLE_PREFIX}{path.name}"
        data = planned.get(relative, path.read_bytes())
        rows.append(f"stable/{path.name}\t{len(data)}\t{sha256_bytes(data)}\r\n")
    if not rows:
        raise DeploymentError("stable update feed is empty")
    return "".join(rows).encode("utf-8")


def update_runtime_manifest(data: bytes, feed_data: bytes, config_data: bytes | None) -> bytes:
    try:
        value = json.loads(data.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DeploymentError("runtime manifest is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise DeploymentError("runtime manifest root must be an object")
    for key in ("native_feed_manifest_length", "native_feed_manifest_sha256"):
        if key not in value:
            raise DeploymentError(f"runtime manifest lacks {key}")
    value["native_feed_manifest_length"] = str(len(feed_data))
    value["native_feed_manifest_sha256"] = sha256_bytes(feed_data)
    if config_data is not None:
        for key in ("config_length", "config_sha256"):
            if key not in value:
                raise DeploymentError(f"runtime manifest lacks {key}")
        value["config_length"] = str(len(config_data))
        value["config_sha256"] = sha256_bytes(config_data)
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def safe_file(root: Path, relative: str, required: bool = True) -> Path | None:
    candidate = (root / Path(relative)).resolve(strict=False)
    if not is_relative_to(candidate, root):
        raise DeploymentError(f"path escapes candidate root: {relative}")
    if not candidate.exists():
        if required:
            raise DeploymentError(f"required candidate file is missing: {relative}")
        return None
    if not candidate.is_file() or file_is_reparse(candidate):
        raise DeploymentError(f"candidate path is not a regular file: {relative}")
    return candidate


def build_plan(
    root: Path,
    address: str,
    reception_port: int,
    game_port: int,
    world_port: int,
    feed_port: int,
    expected_version: str,
    previous_stable_root: Path,
) -> tuple[dict[str, bytes], list[dict[str, object]]]:
    planned: dict[str, bytes] = {}
    native_receipts: list[dict[str, object]] = []
    if not previous_stable_root.is_absolute() or not previous_stable_root.is_dir():
        raise DeploymentError("previous stable feed must be an existing absolute directory")
    if any(file_is_reparse(item) for item in (previous_stable_root, *previous_stable_root.parents)):
        raise DeploymentError("previous stable feed must not traverse a reparse point")
    previous_stable_root = previous_stable_root.resolve(strict=True)
    if is_relative_to(previous_stable_root, root) or is_relative_to(root, previous_stable_root):
        raise DeploymentError("previous hosted feed and new candidate must be independent")
    update_path = safe_file(root, CLIENT_UPDATE)
    area_path = safe_file(root, CLIENT_AREA)
    template_path = safe_file(root, SERVER_TEMPLATE)
    serverlist_path = safe_file(root, STABLE_SERVERLIST)
    patchlist_path = safe_file(root, STABLE_PATCHLIST)
    planned[CLIENT_UPDATE] = transform_update_ini(update_path.read_bytes(), address, feed_port, expected_version)
    planned[CLIENT_AREA] = transform_areaserver(area_path.read_bytes(), address, reception_port, game_port)
    planned[SERVER_TEMPLATE] = transform_server_json(
        template_path.read_bytes(), address, reception_port, game_port, world_port, feed_port, expected_version
    )
    config_path = safe_file(root, SERVER_CONFIG, required=False)
    if config_path is not None:
        planned[SERVER_CONFIG] = transform_server_json(
            config_path.read_bytes(), address, reception_port, game_port, world_port, feed_port, expected_version
        )
    planned[STABLE_SERVERLIST] = transform_areaserver(serverlist_path.read_bytes(), address, reception_port, game_port)
    rewritten_patchlist, entries = transform_patchlist(patchlist_path.read_bytes(), address, feed_port)
    if expected_version not in {version for version, _ in entries}:
        raise DeploymentError(f"patchlist does not contain expected version {expected_version}")
    planned[STABLE_PATCHLIST] = rewritten_patchlist
    stable_root = (root / Path(STABLE_PREFIX)).resolve(strict=True)
    for version, url in entries:
        package_name = PurePosixPath(urllib.parse.urlsplit(url).path).name
        relative = STABLE_PREFIX + package_name
        package_path = safe_file(root, relative)
        before = package_path.read_bytes()
        if version != expected_version:
            expected_history_hash = PUBLISHED_HISTORY.get(version)
            if expected_history_hash is None:
                raise DeploymentError(f"unapproved historical package version: {version}")
            previous_path = safe_file(previous_stable_root, package_name)
            preserved = previous_path.read_bytes()
            if sha256_bytes(preserved) != expected_history_hash:
                raise DeploymentError(f"previous hosted package identity mismatch: {version}")
            preserved_version, preserved_records = parse_native_package(preserved)
            if preserved_version != version:
                raise DeploymentError(f"previous hosted package version mismatch: {version}")
            planned[relative] = preserved
            native_receipts.append({
                "path": relative, "version": version,
                "mode": "COPY_ALREADY_PUBLISHED_BYTES_UNCHANGED",
                "source_path": str(previous_path),
                "source_sha256": expected_history_hash,
                "record_count": len(preserved_records), "changed_payloads": [],
                "before_length": len(before), "before_sha256": sha256_bytes(before),
                "after_length": len(preserved), "after_sha256": expected_history_hash,
            })
            continue
        after, details = transform_native_package(
            before, version, address, feed_port, reception_port, game_port
        )
        planned[relative] = after
        native_receipts.append(
            {
                "path": relative,
                "version": details["version"],
                "record_count": details["record_count"],
                "changed_payloads": details["changed_payloads"],
                "before_length": len(before),
                "before_sha256": sha256_bytes(before),
                "after_length": len(after),
                "after_sha256": sha256_bytes(after),
            }
        )
    feed_data = build_feed_manifest(stable_root, planned)
    planned[FEED_MANIFEST] = feed_data
    runtime_template = safe_file(root, RUNTIME_TEMPLATE)
    planned[RUNTIME_TEMPLATE] = update_runtime_manifest(runtime_template.read_bytes(), feed_data, None)
    runtime_path = safe_file(root, RUNTIME_MANIFEST, required=False)
    if runtime_path is not None:
        if config_path is None:
            raise DeploymentError("runtime-manifest.json exists but config/server.json is absent")
        planned[RUNTIME_MANIFEST] = update_runtime_manifest(
            runtime_path.read_bytes(), feed_data, planned[SERVER_CONFIG]
        )
    return planned, native_receipts


def manifest_bytes(root: Path) -> bytes:
    rows = []
    for relative, path in sorted(enumerate_files(root).items()):
        rows.append(f"F\t{relative}\t{path.stat().st_size}\t{sha256_file(path)}\r\n")
    return "".join(rows).encode("utf-8")


def write_new_directory(directory: Path) -> None:
    if directory.exists():
        raise DeploymentError(f"output directory already exists: {directory}")
    directory.mkdir(parents=True, exist_ok=False)


def atomic_write(path: Path, data: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".ttdm-deploy-", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def execute(args: argparse.Namespace) -> dict[str, object]:
    root = validate_absolute_directory_argument(args.root, "--root")
    source_manifest = Path(args.source_manifest)
    if not source_manifest.is_absolute():
        raise DeploymentError("--source-manifest must be an absolute path")
    source_manifest = source_manifest.resolve(strict=True)
    expected_manifest_hash = args.source_manifest_sha256.upper()
    if SHA256_RE.fullmatch(expected_manifest_hash) is None:
        raise DeploymentError("--source-manifest-sha256 must be 64 hexadecimal characters")
    backup_dir = validate_absolute_directory_argument(args.backup_dir, "--backup-dir")
    receipt_dir = validate_absolute_directory_argument(args.receipt_dir, "--receipt-dir")
    if not root.is_dir():
        raise DeploymentError("--root must name an existing candidate directory")
    for output, name in ((backup_dir, "backup"), (receipt_dir, "receipt")):
        if is_relative_to(output, root) or is_relative_to(root, output):
            raise DeploymentError(f"{name} directory must be independent of the candidate root")
        if output.exists():
            raise DeploymentError(f"{name} directory already exists")
    if backup_dir == receipt_dir or is_relative_to(backup_dir, receipt_dir) or is_relative_to(receipt_dir, backup_dir):
        raise DeploymentError("backup and receipt directories must be independent of each other")
    if is_relative_to(source_manifest, root):
        raise DeploymentError("source manifest must be outside the candidate root")
    address = validate_public_address(args.public_address)
    ports = {
        "reception": validate_port(args.reception_port, "--reception-port"),
        "registration_login": validate_port(args.game_port, "--game-port"),
        "world": validate_port(args.world_port, "--world-port"),
        "native_update_http": validate_port(args.feed_port, "--feed-port"),
    }
    if len(set(ports.values())) != len(ports):
        raise DeploymentError("deployment ports must be distinct")
    validate_version(EXPECTED_VERSION)
    check_cold_with_powershell(root)
    source_manifest_sha256 = sha256_file(source_manifest)
    if source_manifest_sha256 != expected_manifest_hash:
        raise DeploymentError("source manifest SHA-256 does not match the handoff identity")
    source_entries = verify_source_manifest(root, source_manifest)
    seed_identity = source_entries.get(ZERO_PLAYER_SEED)
    if seed_identity != (ZERO_PLAYER_SEED_LENGTH, ZERO_PLAYER_SEED_SHA256):
        raise DeploymentError("candidate is not bound to the sealed zero-player database seed")
    database_residue = sorted(
        relative for relative in source_entries
        if relative.casefold().startswith(ZERO_PLAYER_SEED.casefold()) and relative != ZERO_PLAYER_SEED
    )
    if database_residue:
        raise DeploymentError(f"runtime database sidecars or copies are forbidden: {database_residue!r}")
    before_manifest = manifest_bytes(root)
    planned, native_receipts = build_plan(root, address, **{
        "reception_port": ports["reception"],
        "game_port": ports["registration_login"],
        "world_port": ports["world"],
        "feed_port": ports["native_update_http"],
        "expected_version": EXPECTED_VERSION,
        "previous_stable_root": Path(args.previous_stable_root),
    })
    changes: list[dict[str, object]] = []
    for relative, after in sorted(planned.items()):
        path = safe_file(root, relative)
        before = path.read_bytes()
        if before == after:
            continue
        changes.append(
            {
                "path": relative,
                "before_length": len(before),
                "before_sha256": sha256_bytes(before),
                "after_length": len(after),
                "after_sha256": sha256_bytes(after),
            }
        )
    if not changes:
        raise DeploymentError("requested endpoint projection produces no changes")
    changed_paths = {item["path"] for item in changes}
    permitted = {
        CLIENT_UPDATE, CLIENT_AREA, SERVER_TEMPLATE, SERVER_CONFIG,
        STABLE_SERVERLIST, STABLE_PATCHLIST, FEED_MANIFEST,
        RUNTIME_TEMPLATE, RUNTIME_MANIFEST,
        *(item["path"] for item in native_receipts),
    }
    if not changed_paths <= permitted:
        raise DeploymentError(f"planned change escapes whitelist: {sorted(changed_paths - permitted)!r}")
    write_new_directory(backup_dir)
    files_backup = backup_dir / "files"
    for item in changes:
        relative = str(item["path"])
        destination = files_backup / Path(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / Path(relative), destination)
    (backup_dir / "source-manifest.tsv").write_bytes(before_manifest)
    backup_manifest = manifest_bytes(backup_dir)
    (backup_dir / "backup-manifest.tsv").write_bytes(backup_manifest)
    mutation_started = False
    try:
        for relative, after in planned.items():
            if relative in changed_paths:
                mutation_started = True
                atomic_write(root / Path(relative), after)
        after_manifest = manifest_bytes(root)
        actual_files = enumerate_files(root)
        if set(actual_files) != set(source_entries):
            raise DeploymentError("file set changed during projection")
        actual_changes = []
        for relative, (before_length, before_hash) in source_entries.items():
            path = actual_files[relative]
            after_length = path.stat().st_size
            after_hash = sha256_file(path)
            if (before_length, before_hash) != (after_length, after_hash):
                actual_changes.append(relative)
        if set(actual_changes) != changed_paths:
            raise DeploymentError("post-write full-manifest diff does not equal the planned whitelist")
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "tool_schema": TOOL_SCHEMA,
            "status": "OFFLINE_ENVIRONMENT_PROJECTION_COMPLETE",
            "root": str(root),
            "source_manifest": {
                "path": str(source_manifest),
                "length": source_manifest.stat().st_size,
                "sha256": source_manifest_sha256,
                "matched_before_write": True,
            },
            "expected_version": EXPECTED_VERSION,
            "endpoint": {"public_address": address, **ports},
            "cold_state": "PASS_BY_READ_ONLY_PROCESS_PATH_CHECK",
            "changed_files": changes,
            "native_packages": native_receipts,
            "full_file_set_preserved": True,
            "full_diff_equals_whitelist": True,
            "backup": {
                "path": str(backup_dir),
                "backup_manifest_sha256": sha256_bytes(backup_manifest),
            },
            "runtime_started": False,
            "network_requested": False,
            "database_modified_or_copied": False,
            "zero_player_seed_hashed_read_only": {
                "path": ZERO_PLAYER_SEED,
                "length": ZERO_PLAYER_SEED_LENGTH,
                "sha256": ZERO_PLAYER_SEED_SHA256,
            },
            "business_program_compiled_or_modified": False,
        }
        receipt_data = (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        result = {
            "status": receipt["status"],
            "root": str(root),
            "changed_file_count": len(changes),
            "native_package_count": len(native_receipts),
            "receipt": str(receipt_dir / "repack-receipt.json"),
            "receipt_sha256": sha256_bytes(receipt_data),
        }
        write_new_directory(receipt_dir)
        (receipt_dir / "before-manifest.tsv").write_bytes(before_manifest)
        (receipt_dir / "after-manifest.tsv").write_bytes(after_manifest)
        atomic_write(receipt_dir / "repack-receipt.json", receipt_data)
        return result
    except Exception as exc:
        if not mutation_started:
            raise
        restore_errors: list[str] = []
        for item in changes:
            relative = str(item["path"])
            saved = files_backup / Path(relative)
            try:
                if saved.stat().st_size != item["before_length"] or sha256_file(saved) != item["before_sha256"]:
                    raise DeploymentError("backup identity mismatch")
                atomic_write(root / Path(relative), saved.read_bytes())
                restored = root / Path(relative)
                if restored.stat().st_size != item["before_length"] or sha256_file(restored) != item["before_sha256"]:
                    raise DeploymentError("restored identity mismatch")
            except Exception as restore_exc:
                restore_errors.append(f"{relative}: {restore_exc}")
        try:
            if manifest_bytes(root) != before_manifest:
                restore_errors.append("restored full-tree manifest does not match the preimage")
        except Exception as verify_exc:
            restore_errors.append(f"restored full-tree verification failed: {verify_exc}")
        failed_attempt = {
            "schema": "ttdm.deployment-repack-failed-attempt.v1",
            "status": "ROLLED_BACK" if not restore_errors else "ROLLBACK_INCOMPLETE",
            "root": str(root),
            "error_type": type(exc).__name__,
            "error": str(exc),
            "changed_paths": sorted(changed_paths),
            "restored_byte_exact": not restore_errors,
            "restore_errors": restore_errors,
            "success_receipt_committed": False,
        }
        (backup_dir / "FAILED-ATTEMPT.json").write_text(
            json.dumps(failed_attempt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        status = failed_attempt["status"]
        raise DeploymentError(f"deployment projection failed; {status}: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="absolute path of a fresh extracted complete product root")
    parser.add_argument("--source-manifest", required=True, help="absolute path of its sealed package-full-manifest.tsv")
    parser.add_argument("--source-manifest-sha256", required=True, help="handoff SHA-256 of the sealed source manifest")
    parser.add_argument("--previous-stable-root", required=True, help="absolute directory of the existing published v137 stable feed; read-only hash-bound historical DT source")
    parser.add_argument("--public-address", required=True, help="globally routable IP or explicit public hostname")
    parser.add_argument("--reception-port", type=int, default=6666)
    parser.add_argument("--game-port", type=int, default=10003)
    parser.add_argument("--world-port", type=int, default=12000)
    parser.add_argument("--feed-port", type=int, default=18080)
    parser.add_argument("--backup-dir", required=True, help="new absolute directory outside root")
    parser.add_argument("--receipt-dir", required=True, help="new absolute directory outside root")
    return parser


def main() -> int:
    try:
        result = execute(build_parser().parse_args())
    except (DeploymentError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "REFUSED", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

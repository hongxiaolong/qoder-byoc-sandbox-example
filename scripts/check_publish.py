#!/usr/bin/env python3
"""Offline Git publication guard. Reports locations, never credential values.

Checks current publishable files and index; --history also scans every reachable
Git blob. This is a preventive guard, not a proof that arbitrary data is harmless.
"""
import argparse
import base64
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import quote

from dotenv import dotenv_values


PATTERNS = {
    "Aliyun AccessKey": rb"\bLTAI[A-Za-z0-9]{12,32}\b",
    "AWS AccessKey": rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    "GitHub token": rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b",
    "E2B token": rb"\be2b_[A-Za-z0-9_-]{24,}\b",
    "API token": rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{30,}\b",
    "private key": rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
}
PATTERNS = {name: re.compile(pattern) for name, pattern in PATTERNS.items()}


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.PIPE)


def private_path(name):
    path = Path(name)
    if any(p in {"var", ".venv", "__pycache__", ".aws", ".ssh", ".docker"} or p.endswith(".egg-info") for p in path.parts):
        return True
    if path.name.startswith(".env") and not path.name.endswith(".example"):
        return True
    if name.startswith(("config/registry.toml", "config/worker.json")):
        return True
    return path.suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".jks"} or path.name == "credentials.json" or path.name.startswith("service-account") and path.suffix == ".json"


def known_credentials(root):
    values = {}
    for name in [".env", ".env.cloud", ".env.registry"]:
        for key, value in dotenv_values(root / "config" / name, interpolate=False).items():
            if value and len(value) >= 8 and any(word in key for word in ["TOKEN", "SECRET", "PASSWORD", "API_KEY", "ACCESS_KEY_ID"]):
                for variant in {value, quote(value, safe=""), base64.b64encode(value.encode()).decode()}:
                    values[variant.encode()] = key
    return values


def audit(root, history=False):
    root = Path(root).resolve()
    secrets = known_credentials(root)
    findings = set()
    counts = {"working_files": 0, "index_files": 0, "history_blobs": 0}

    def scan(data, scope, name):
        if private_path(name):
            findings.add((scope, name, 0, "private/generated path"))
        for value, key in secrets.items():
            at = data.find(value)
            if at >= 0:
                findings.add((scope, name, data[:at].count(b"\n") + 1, "local credential: " + key))
        for label, pattern in PATTERNS.items():
            for match in pattern.finditer(data):
                findings.add((scope, name, data[:match.start()].count(b"\n") + 1, label))

    paths = set(git(root, "ls-files", "-z").decode().split("\0"))
    paths.update(git(root, "ls-files", "--others", "--exclude-standard", "-z").decode().split("\0"))
    for name in sorted(paths - {""}):
        path = root / name
        if path.is_symlink():
            scan(os.readlink(path).encode(), "working", name)
        elif path.is_file():
            scan(path.read_bytes(), "working", name)
        counts["working_files"] += 1
    for entry in git(root, "ls-files", "--stage", "-z").decode().split("\0"):
        if not entry:
            continue
        metadata, name = entry.split("\t", 1)
        _, oid, _ = metadata.split()
        scan(git(root, "cat-file", "blob", oid), "index", name)
        counts["index_files"] += 1
    if history:
        for entry in git(root, "rev-list", "--objects", "--all").decode().splitlines():
            oid, _, name = entry.partition(" ")
            if git(root, "cat-file", "-t", oid).strip() == b"blob":
                scan(git(root, "cat-file", "blob", oid), "history", name or oid[:12])
                counts["history_blobs"] += 1
    return sorted(findings), counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        findings, counts = audit(args.root, args.history)
    except (OSError, subprocess.CalledProcessError):
        parser.exit(2, "Publication check could not complete; verify repository access.\n")
    for scope, name, line, kind in findings:
        print(f"BLOCK [{scope}] {name}:{line} ({kind})")
    if findings:
        parser.exit(1, "Credentials/private artifacts may be published. Values were not printed.\n")
    print("PASS: publication scan; " + ", ".join(f"{key}={value}" for key, value in counts.items()))
    print("No detected credential matches. Review screenshots and do not force-add local secrets.")


if __name__ == "__main__":
    main()

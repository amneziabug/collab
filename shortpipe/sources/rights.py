"""Authorization gate: every video must carry an accepted license and a rights
holder before it may be processed or uploaded."""

from __future__ import annotations

from typing import Mapping


def check_rights(item: Mapping, allowed_licenses: list[str]) -> tuple[bool, str]:
    license_ = (item.get("license") or "").lower().strip()
    if not license_:
        return False, "no license declared"
    if license_ not in {l.lower() for l in allowed_licenses}:
        return False, f"license {license_!r} not in allowed list"
    if not (item.get("rights_holder") or "").strip():
        return False, "no rights holder declared"
    if license_ == "cc-by" and not (item.get("attribution") or "").strip():
        return False, "cc-by requires an attribution line"
    file_path = item.get("file_path")
    if file_path and "://" in str(file_path):
        return False, "remote file URLs are not accepted; download authorized files locally first"
    return True, "ok"

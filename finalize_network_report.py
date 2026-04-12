
from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import certifi

    _CA = certifi.where()
except Exception:
    _CA = None


def _appdata_cache_dir() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "ux-ComputerCache"


def _pending_path() -> Path:
    return _appdata_cache_dir() / "network_report_pending.json"


def _mac_for_guid(guid: str) -> Optional[str]:
    g = (guid or "").strip()
    if not g:
        return None
    esc = g.replace("'", "''")
    cmd = (
        f"$a = Get-NetAdapter | Where-Object {{ $_.InterfaceGuid -eq '{esc}' }} | Select-Object -First 1; "
        "if ($null -eq $a) { 'null' } else { ($a.MacAddress | Out-String).Trim() }"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True,
            text=True,
            timeout=45,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        t = (r.stdout or "").strip()
        if not t or t == "null":
            return None
        return t
    except Exception:
        return None


def _normalize_mac_dashed(mac: str) -> str:
    s = (mac or "").strip().upper().replace(":", "").replace("-", "").replace(" ", "")
    if len(s) != 12:
        return (mac or "").strip().upper()
    return "-".join(s[i : i + 2] for i in range(0, 12, 2))


def _post_json(url: str, payload: Dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    ctx = ssl.create_default_context(cafile=_CA) if _CA else ssl.create_default_context()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, context=ctx, timeout=45) as resp:
        if resp.status not in (200, 201):
            raise RuntimeError(f"HTTP {resp.status}")


def main() -> int:
    pending = _pending_path()
    if not pending.is_file():
        return 0
    try:
        data = json.loads(pending.read_text(encoding="utf-8"))
    except Exception:
        return 1
    if not isinstance(data, dict):
        return 1

    report_id = str(data.get("report_id") or "").strip()
    license_key = str(data.get("license_key") or "").strip()
    hwid = str(data.get("hwid") or "").strip()
    adapter_name = str(data.get("adapter_name") or "").strip()
    adapter_brand = str(data.get("adapter_brand") or "").strip()
    previous_mac = str(data.get("previous_mac") or "").strip()
    change_time = str(data.get("change_time") or "").strip()
    method = str(data.get("method") or "wmac").strip().lower()
    base_url = str(data.get("base_url") or "https://joonysoftware.xyz").rstrip("/")
    guid = str(data.get("interface_guid") or "").strip()

    if not report_id or not license_key or not hwid:
        return 1

    current_raw = _mac_for_guid(guid)
    if not current_raw:
        return 2

    current_mac = _normalize_mac_dashed(current_raw)
    previous_mac_n = _normalize_mac_dashed(previous_mac)
    if not adapter_name:
        adapter_name = "Network adapter"
    if not adapter_brand:
        adapter_brand = "Unknown"
    if method not in ("wmac", "registry"):
        method = "wmac"
    if not change_time:
        return 1

    url = f"{base_url}/api/loader/network-report"
    payload = {
        "reportId": report_id,
        "licenseKey": license_key,
        "hwid": hwid,
        "username": data.get("username"),
        "adapterName": adapter_name,
        "adapterBrand": adapter_brand,
        "previousMac": previous_mac_n,
        "currentMac": current_mac,
        "changeTime": change_time,
        "method": method,
    }
    try:
        _post_json(url, payload)
    except Exception:
        return 3

    try:
        pending.unlink()
    except Exception:
        pass

    report_url = f"{base_url}/networkreport?reportId={report_id}"
    try:
        os.startfile(report_url)  # type: ignore[attr-defined]
    except Exception:
        try:
            subprocess.Popen(  # noqa: S603
                ["cmd", "/c", "start", "", report_url],
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

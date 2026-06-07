
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

# WAFs often block Python-urllib; mimic a desktop browser (same idea as loader requests).
_HTTP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36 JoonyNetworkReportFinalize/1"
)


def _appdata_cache_dir() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "ux-ComputerCache"


def _pending_path() -> Path:
    return _appdata_cache_dir() / "network_report_pending.json"


def _lookup_adapter_mac_via_powershell(guid: str, interface_description: str) -> Optional[str]:
    """
    Resolve current MAC after reboot. String -eq on InterfaceGuid often fails (brace/casing/format);
    use [guid]::TryParse, then fall back to InterfaceDescription match.
    """
    env = {**os.environ, "_JOONY_NR_GUID": (guid or "").strip(), "_JOONY_NR_DESC": (interface_description or "").strip()}
    ps = r"""
$guidStr = $env:_JOONY_NR_GUID
$desc = $env:_JOONY_NR_DESC
$mac = $null
function Get-Mac($na) {
  if ($null -eq $na) { return $null }
  $m = $na.MacAddress
  if ($null -eq $m) { return $null }
  $s = ($m | Out-String).Trim()
  if (-not $s) { return $null }
  return $s
}
$all = @(Get-NetAdapter -ErrorAction SilentlyContinue)
# 1) GUID: parse then compare as [guid] (avoids string -eq failures)
if ($guidStr -and $guidStr.Trim()) {
  $parsed = $null
  if ([guid]::TryParse($guidStr.Trim(), [ref]$parsed)) {
    $hit = $all | Where-Object { $_.HardwareInterface -eq $true -and $_.InterfaceGuid -eq $parsed } | Select-Object -First 1
    $mac = Get-Mac $hit
    if (-not $mac) {
      $hit = $all | Where-Object { $_.InterfaceGuid -eq $parsed } | Select-Object -First 1
      $mac = Get-Mac $hit
    }
    if (-not $mac) {
      $want = $parsed.ToString('D').ToLowerInvariant()
      $hit = $all | Where-Object {
        try { $_.InterfaceGuid.ToString('D').ToLowerInvariant() -eq $want } catch { $false }
      } | Select-Object -First 1
      $mac = Get-Mac $hit
    }
  }
}
# 2) Exact InterfaceDescription from pending JSON
if (-not $mac -and $desc -and $desc.Trim()) {
  $hit = $all | Where-Object { $_.InterfaceDescription -eq $desc.Trim() } | Select-Object -First 1
  $mac = Get-Mac $hit
}
if ($mac) { Write-Output $mac } else { Write-Output 'null' }
""".strip()
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env=env,
        )
        t = (r.stdout or "").strip().splitlines()
        line = t[-1].strip() if t else ""
        if not line or line == "null":
            return None
        return line
    except Exception:
        return None


def _normalize_mac_dashed(mac: str) -> str:
    s = (mac or "").strip().upper().replace(":", "").replace("-", "").replace(" ", "")
    if len(s) != 12:
        return (mac or "").strip().upper()
    return "-".join(s[i : i + 2] for i in range(0, 12, 2))


def _post_network_report(base_url: str, payload: Dict[str, Any]) -> None:
    """
    POST to Next `/api/network-report` first; on 405/404 (old deploy or static rules) retry
    `/api/loader/network-report` which proxies to Express.
    """
    body = json.dumps(payload).encode("utf-8")
    base = base_url.rstrip("/")
    urls = list(
        dict.fromkeys(
            (
                f"{base}/api/network-report",
                f"{base}/api/loader/network-report",
            )
        )
    )

    ctx = ssl.create_default_context(cafile=_CA) if _CA else ssl.create_default_context()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": _HTTP_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    last_err: Optional[BaseException] = None
    for i, url in enumerate(urls):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=45) as resp:
                if resp.status in (200, 201):
                    print(f"Network report POST OK: HTTP {resp.status}")
                    return
                raise RuntimeError(f"HTTP {resp.status}")
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (404, 405) and i < len(urls) - 1:
                print(f"[Joony] POST {url} → HTTP {e.code}, trying fallback URL…")
                continue
            raise RuntimeError(f"HTTP Error {e.code}: {e.reason}") from e
        except Exception as e:
            last_err = e
            if i < len(urls) - 1:
                print(f"[Joony] POST {url} failed ({e!r}), trying fallback URL…")
                continue
            raise
    if last_err:
        raise RuntimeError(str(last_err)) from last_err


def main() -> int:
    pending = _pending_path()
    if not pending.is_file():
        return 0
    try:
        data = json.loads(pending.read_text(encoding="utf-8"))
    except Exception:
        print("Error loading pending network report data")
        return 1
    if not isinstance(data, dict):
        print("Pending network report data is not a dictionary")
        return 1

    report_id = str(data.get("report_id") or "").strip()
    license_key = str(data.get("license_key") or "").strip()
    hwid = str(data.get("hwid") or "").strip()
    adapter_name = str(data.get("adapter_name") or "").strip()
    adapter_brand = str(data.get("adapter_brand") or "").strip()
    previous_mac = str(data.get("previous_mac") or "").strip()
    change_time = str(data.get("change_time") or "").strip()
    method = str(data.get("method") or "wmac").strip().lower()
    base_url = str(data.get("base_url") or "http://localhost:4000").rstrip("/")
    guid = str(data.get("interface_guid") or "").strip()

    if not report_id or not license_key or not hwid:
        print("Missing required fields")
        return 1

    current_raw = _lookup_adapter_mac_via_powershell(guid, adapter_name)
    if not current_raw:
        print(
            "No current MAC found (GUID and InterfaceDescription lookup failed). "
            f"guid={guid!r}, adapter_name={adapter_name!r}"
        )
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
        print("Missing change time")
        return 1

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
        _post_network_report(base_url, payload)
    except Exception as error:
        print(f"Error posting network report: {error}")
        return 3

    try:
        pending.unlink()
    except Exception as error:
        print(f"Error unlinking pending network report: {error}")
        pass

    report_url = f"{base_url}/networkreport?reportId={report_id}"
    try:
        print(f"Opening report URL: {report_url}")
        subprocess.Popen(  # noqa: S603
            ["cmd", "/c", "start", "", report_url],
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as error:
        print(f"Error opening report URL: {error}")
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

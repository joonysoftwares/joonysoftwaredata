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

_LOG_PREFIX = "[JOONY SOFTWARE]"

_HTTP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36 JoonyAsusReportFinalize/1"
)


def _log(message: str) -> None:
    print(f"{_LOG_PREFIX} Log | {message}")


def _appdata_cache_dir() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "ux-ComputerCache"


def _pending_path() -> Path:
    return _appdata_cache_dir() / "asus_report_pending.json"


def _collect_serials_via_powershell() -> Dict[str, Any]:
    ps = r"""
$b    = Get-CimInstance -ClassName Win32_BaseBoard           -ErrorAction SilentlyContinue | Select-Object -First 1
$p    = Get-CimInstance -ClassName Win32_ComputerSystemProduct -ErrorAction SilentlyContinue | Select-Object -First 1
$bios = Get-CimInstance -ClassName Win32_BIOS                -ErrorAction SilentlyContinue | Select-Object -First 1
@{
  BoardManufacturer = if ($b)    { $b.Manufacturer }           else { $null }
  BoardProduct      = if ($b)    { $b.Product }                else { $null }
  BoardSerial       = if ($b)    { $b.SerialNumber }           else { $null }
  SystemUUID        = if ($p)    { $p.UUID }                   else { $null }
  SystemSerial      = if ($p)    { $p.IdentifyingNumber }      else { $null }
  BiosSerial        = if ($bios) { $bios.SerialNumber }        else { $null }
} | ConvertTo-Json -Compress
""".strip()
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        raw = (r.stdout or "").strip()
        if raw:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
    except Exception as e:
        _log(f"ASUS report: serial collection failed ({e})")
    return {}


def _post_asus_report(base_url: str, payload: Dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    base = base_url.rstrip("/")
    urls = list(dict.fromkeys((
        f"{base}/api/asus-report",
        f"{base}/api/loader/asus-report",
    )))

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
                    _log(f"ASUS report: submitted successfully (HTTP {resp.status})")
                    return
                raise RuntimeError(f"HTTP {resp.status}")
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (404, 405) and i < len(urls) - 1:
                _log(
                    f"ASUS report: primary endpoint returned HTTP {e.code}; "
                    f"retrying with alternate API path"
                )
                continue
            raise RuntimeError(f"HTTP Error {e.code}: {e.reason}") from e
        except Exception as e:
            last_err = e
            if i < len(urls) - 1:
                _log(
                    f"ASUS report: request to primary endpoint failed ({e!r}); "
                    f"retrying with alternate API path"
                )
                continue
            raise
    if last_err:
        raise RuntimeError(str(last_err)) from last_err


def main() -> int:
    pending_file = _pending_path()
    if not pending_file.is_file():
        return 0

    try:
        data = json.loads(pending_file.read_text(encoding="utf-8"))
    except Exception:
        _log("ASUS report: could not read pending data file; aborting")
        return 1
    if not isinstance(data, dict):
        _log("ASUS report: pending data is not a valid object; aborting")
        return 1

    report_id = str(data.get("report_id") or "").strip()
    license_key = str(data.get("license_key") or "").strip()
    hwid = str(data.get("hwid") or "").strip()
    if not report_id or not license_key or not hwid:
        _log("ASUS report: missing required fields (report_id, license_key, or hwid); aborting")
        return 1

    change_time = str(data.get("change_time") or "").strip()
    if not change_time:
        _log("ASUS report: missing change_time; aborting")
        return 1

    base_url = str(data.get("base_url") or "https://joonysoftware.xyz").rstrip("/")

    prev_board_serial = str(data.get("previous_board_serial") or "").strip() or None
    prev_system_uuid = str(data.get("previous_system_uuid") or "").strip() or None
    prev_system_serial = str(data.get("previous_system_serial") or "").strip() or None
    prev_bios_serial = str(data.get("previous_bios_serial") or "").strip() or None

    serials = _collect_serials_via_powershell()
    cur_board_serial = str(serials.get("BoardSerial") or "").strip() or None
    cur_system_uuid = str(serials.get("SystemUUID") or "").strip() or None
    cur_system_serial = str(serials.get("SystemSerial") or "").strip() or None
    cur_bios_serial = str(serials.get("BiosSerial") or "").strip() or None
    mb_manufacturer = str(serials.get("BoardManufacturer") or "").strip() or None
    mb_product = str(serials.get("BoardProduct") or "").strip() or None

    payload: Dict[str, Any] = {
        "reportId": report_id,
        "licenseKey": license_key,
        "hwid": hwid,
        "username": data.get("username"),
        "changeTime": change_time,
        "motherboardManufacturer": mb_manufacturer,
        "motherboardProduct": mb_product,
        "previousBoardSerial": prev_board_serial,
        "currentBoardSerial": cur_board_serial,
        "previousSystemUuid": prev_system_uuid,
        "currentSystemUuid": cur_system_uuid,
        "previousSystemSerial": prev_system_serial,
        "currentSystemSerial": cur_system_serial,
        "previousBiosSerial": prev_bios_serial,
        "currentBiosSerial": cur_bios_serial,
    }

    try:
        _post_asus_report(base_url, payload)
    except Exception as err:
        _log(f"ASUS report: submission failed ({err})")
        return 3

    try:
        pending_file.unlink()
    except Exception as err:
        _log(f"ASUS report: could not remove pending file ({err})")

    report_url = f"{base_url}/asusreport?reportId={report_id}"
    try:
        _log(f"ASUS report: opening report in default browser ({report_url})")
        subprocess.Popen(
            ["cmd", "/c", "start", "", report_url],
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as err:
        _log(f"ASUS report: could not launch browser ({err}); retrying once")
        try:
            subprocess.Popen(
                ["cmd", "/c", "start", "", report_url],
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            _log("ASUS report: browser launch retry failed; open the report URL manually")

    return 0


if __name__ == "__main__":
    sys.exit(main())

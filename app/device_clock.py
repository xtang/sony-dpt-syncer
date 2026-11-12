"""DPT's datetime endpoint is write-only; read its clock from the HTTP Date header."""
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


def read_clock(device):
    response = device._get_endpoint("/system/configs/timezone")
    response.raise_for_status()
    value = response.headers.get("Date")
    if not value:
        raise RuntimeError("设备未返回时间信息")
    normalized = re.sub(r"(?:GMT|UTC)([+-])(\d{2}):?(\d{2})$", r"\1\2\3", value)
    parsed = parsedate_to_datetime(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z"), parsed


def synchronize_clock(device, reauthenticate=None):
    before, _ = read_clock(device)
    clock = datetime.now(timezone.utc).replace(microsecond=0)
    response = device._put_endpoint("/system/configs/datetime", data={"value": clock.isoformat().replace("+00:00", "Z")})
    response.raise_for_status()
    if reauthenticate:
        reauthenticate()
    after, actual = read_clock(device)
    difference = abs((datetime.now(timezone.utc)-actual).total_seconds())
    if difference > 15:
        raise RuntimeError("设备时间校验失败，请重试")
    return dict(before=before, after=after, difference_seconds=round(difference, 2))

"""Channel descriptions and inferred display units.

The reference JSON contains descriptions, not units. Explicit overrides win,
then units are inferred from descriptions and channel names.
"""
from __future__ import annotations

import json
import os
import re
import tempfile

_DIR = os.path.dirname(os.path.abspath(__file__))
DESC_FILE = os.path.join(_DIR, "channel_descriptions.json")
OVERRIDE_FILE = os.path.join(_DIR, "channel_units_overrides.json")

_BOOL_WORDS = ("flag", " active", " status", "error", "fault", "switch",
               "enable", "disable", "indication", "diag", "valid", "timeout",
               "version", "serial", "counter", "testsignal", "state", "quality",
               "1 =", "0 =", "bit coded", "warning", "alarm", "available",
               "present", "on/off")
_DESC_RULES = [
    (("temperature", "temp "), "C"),
    (("yaw rate", "roll rate", "pitch rate"), "deg/s"),
    (("pressure",), "bar"),
    (("engine speed", "revolution"), "rpm"),
    (("wheel speed", "vehicle speed", "ground speed", "car speed"), "km/h"),
    (("angle",), "deg"),
    (("voltage",), "V"),
    (("current",), "A"),
    (("torque",), "Nm"),
    (("lambda",), "lambda"),
    (("acceleration",), "G"),
    (("distance",), "m"),
    (("percentage", "percent", "duty", "throttle", " slip"), "%"),
    (("gear",), ""),
]

_NAME_DIMENSIONLESS = [
    re.compile(r"^B[A-Z_]|_B[A-Z_]|_b[A-Z_]|^b[A-Z]"),
    re.compile(r"(?:Cnt|Counter|_Sts|Status|State|Flag|Diag|"
               r"Active|Enable|Enad|Avail|Avl|Mode|Sel|Select|"
               r"Version|Serial|Err(?:or)?)\d*$"),
]
_NAME_SPECIAL = [
    (re.compile(r"[nN](?:Yaw|Roll|Pitch)"), "deg/s"),
    (re.compile(r"(?:^|_)[Tt](?:Water\d*|Oil(?:Sump)?|Airbox|Exhaust|Manifold)(?:$|_)"), "C"),
    (re.compile(r"(?:^|_)(?:RPM|nFanRPM)(?:$|_)"), "rpm"),
    (re.compile(r"Lambda"), "lambda"),
    (re.compile(r"VBatt|Voltage"), "V"),
    (re.compile(r"Current"), "A"),
    (re.compile(r"Torque|_Tq$|TqReal|TorqueReal"), "Nm"),
    (re.compile(r"[nN]Gear"), ""),
    (re.compile(r"DistanceLap|^sLap"), "m"),
    (re.compile(r"tLap|Laptime"), "s"),
    (re.compile(r"GPS_(?:Latitude|Longitude)$"), "deg"),
    (re.compile(r"^(?:vCar|CarSpd_vCar|GPS_CarSpeed)$"), "km/h"),
    (re.compile(r"^(?:gLat|gLong)$"), "G"),
    (re.compile(r"^(?:rPedal|rThrottle\d*)$"), "%"),
]


def infer_unit_from(name: str, desc: str | None):
    if any(rx.search(name) for rx in _NAME_DIMENSIONLESS):
        return "", "name"
    for rx, unit in _NAME_SPECIAL:
        if rx.search(name):
            return unit, "name"
    d = (desc or "").lower()
    if d:
        if any(w in d for w in _BOOL_WORDS):
            return "", "desc"
        for words, unit in _DESC_RULES:
            if any(w in d for w in words):
                return unit, "desc"
    return None, "none"


def group(name: str) -> str:
    """Group channels by module prefix."""
    return name.split("_", 1)[0] if "_" in name else "General"


def _load_map(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}


class Catalog:
    group = staticmethod(group)

    def __init__(self):
        self.desc = _load_map(DESC_FILE)
        self.overrides = _load_map(OVERRIDE_FILE)

    def description(self, name: str) -> str:
        return self.desc.get(name, "")

    def unit(self, name: str):
        """Return (unit, source). unit is '' for dimensionless, None if unknown."""
        if name in self.overrides:
            return self.overrides[name], "override"
        return infer_unit_from(name, self.desc.get(name))

    def set_unit(self, name: str, unit: str):
        overrides = {**self.overrides, name: unit}
        temp_path = ""
        try:
            with tempfile.NamedTemporaryFile(
                    "w", encoding="utf-8",
                    dir=os.path.dirname(OVERRIDE_FILE) or ".", delete=False) as f:
                temp_path = f.name
                json.dump(overrides, f, indent=0, ensure_ascii=False)
            os.replace(temp_path, OVERRIDE_FILE)
        finally:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)
        self.overrides = overrides

    def label(self, name: str) -> str:
        u, _ = self.unit(name)
        if u:
            return f"{name} [{u}]"
        return name

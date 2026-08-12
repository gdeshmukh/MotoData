"""
catalog.py -- channel metadata: human-readable descriptions and units.

Units are not stored in the logger session files (the channel table holds a
quantity-id, not a unit string), and the channel-description reference has only
names + descriptions. So units are *inferred*:

    1. user override   (channel_units_overrides.json)  -- always wins
    2. description keyword match   (from the description text)
    3. channel-name Hungarian-prefix convention   (fallback)

Anything still unknown is returned as None so the UI can ask the user to fill it
in (the answer is saved back to the override file and reused forever).

Unit spellings use a standard motorsport vocabulary:
    km/h rpm bar C deg deg/s mm m s % V A Nm N G m/s2 lambda ratio Hz kg L
"""
from __future__ import annotations
import os, re, json

_DIR = os.path.dirname(os.path.abspath(__file__))
DESC_FILE = os.path.join(_DIR, "channel_descriptions.json")
OVERRIDE_FILE = os.path.join(_DIR, "channel_units_overrides.json")

# ---- description keyword -> unit (checked in order; first hit wins) ----
# Booleans / enums / diagnostics first so "pressure sensor status" != bar.
_BOOL_WORDS = ("flag", "active", " status", "error", "fault", "request",
               "switch", "enable", "disable", "indication", "diag", "valid",
               "timeout", "version", "serial", "counter", "number", " mode",
               "selected", " map ", "state", "quality", "1 =", "0 =", "bit",
               "warning", "alarm", "available", "present", "on/off")
_DESC_RULES = [
    (("temperature", "temp "), "C"),
    (("yaw rate", "roll rate", "pitch rate"), "deg/s"),
    (("pressure",), "bar"),
    (("engine speed", "rpm", "revolution"), "rpm"),
    (("wheel speed", "vehicle speed", "ground speed", "speed"), "km/h"),
    (("angle",), "deg"),
    (("voltage",), "V"),
    (("current",), "A"),
    (("torque",), "Nm"),
    (("lambda",), "lambda"),
    (("acceleration",), "g"),
    (("distance",), "m"),
    (("percentage", "percent", "duty", "throttle", "pedal", " slip"), "%"),
    (("gear",), ""),          # gear number: dimensionless
]

# ---- Hungarian type-letter -> unit (fallback when desc gives nothing) ----
_NAME_RULES = {
    "v": "km/h", "p": "bar", "T": "C", "a": "deg", "n": "rpm",
    "r": "%", "x": "mm", "s": "m", "I": "A", "U": "V", "g": "G", "t": "s",
}
# obvious whole-name special cases (checked in order, before the type-letter rule)
_NAME_SPECIAL = [
    (re.compile(r"[nN](?:Yaw|Roll|Pitch)"), "deg/s"),       # body rates, not rpm
    (re.compile(r"t(?:Water|Oil|Air|Exh|Intake|Manifold)"), "C"),  # lc-t temps
    (re.compile(r"(?:^|_)RPM$|FanRPM"), "rpm"),
    (re.compile(r"Lambda"), "lambda"),
    (re.compile(r"VBatt|Voltage"), "V"),
    (re.compile(r"Current"), "A"),
    (re.compile(r"Torque|_Tq$|TqReal|TorqueReal"), "Nm"),
    (re.compile(r"[nN]Gear"), ""),       # gear -> dimensionless
    (re.compile(r"DistanceLap|^sLap"), "m"),
    (re.compile(r"tLap"), "s"),
    # booleans / counters / enums / diagnostics -> dimensionless ('')
    (re.compile(r"^B[A-Z_]|_B[A-Z_]|_b[A-Z_]|^b[A-Z]"), ""),
    (re.compile(r"(?:Cnt|Counter|_Sts|Status|State|Flag|Diag|"
                r"Active|Enable|Enad|Avail|Avl|Mode|Sel|Select|"
                r"Version|Serial|Err)\d*$"), ""),
]


def _type_letter(name: str):
    # strip a leading MODULE_ prefix (ABS_, ENG_, Gbx_, EPS_, ...)
    core = re.sub(r"^[A-Za-z]{2,}[_]", "", name)
    m = re.match(r"([a-zA-Z])[A-Z]", core)   # type letter then capitalised word
    return m.group(1) if m else None


def infer_unit_from(name: str, desc: str | None):
    d = (desc or "").lower()
    if d:
        if any(w in d for w in _BOOL_WORDS):
            # still allow a strong quantity word to win (e.g. "brake pressure error")
            for words, unit in _DESC_RULES[:11]:   # numeric quantities only
                if any(w in d for w in words):
                    return unit, "desc"
            return "", "desc"
        for words, unit in _DESC_RULES:
            if any(w in d for w in words):
                return unit, "desc"
    for rx, unit in _NAME_SPECIAL:
        if rx.search(name):
            return unit, "name"
    t = _type_letter(name)
    if t in _NAME_RULES:
        return _NAME_RULES[t], "name"
    return None, "none"


def group(name: str) -> str:
    """Channel group, taken from the name's module prefix (data-derived, so it
    works for any car). Bare names with no prefix fall in 'General'."""
    return name.split("_", 1)[0] if "_" in name else "General"


class Catalog:
    group = staticmethod(group)

    def __init__(self):
        self.desc = {}
        if os.path.exists(DESC_FILE):
            self.desc = json.load(open(DESC_FILE, encoding="utf-8"))
        self.overrides = {}
        if os.path.exists(OVERRIDE_FILE):
            try:
                self.overrides = json.load(open(OVERRIDE_FILE, encoding="utf-8"))
            except Exception:
                self.overrides = {}

    def description(self, name: str) -> str:
        return self.desc.get(name, "")

    def unit(self, name: str):
        """Return (unit, source). unit is '' for dimensionless, None if unknown."""
        if name in self.overrides:
            return self.overrides[name], "override"
        return infer_unit_from(name, self.desc.get(name))

    def set_unit(self, name: str, unit: str):
        self.overrides[name] = unit
        json.dump(self.overrides, open(OVERRIDE_FILE, "w", encoding="utf-8"),
                  indent=0, ensure_ascii=False)

    def label(self, name: str) -> str:
        u, _ = self.unit(name)
        if u:
            return f"{name} [{u}]"
        return name

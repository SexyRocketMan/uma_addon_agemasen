"""Shape key (morph) naming.

UmaViewer can spell morph names in three ways, and an operator here should not care which one the
model it is looking at happens to use:

    tagged   EyeBrow_1_R(WaraiA)[M_Face]   stock UmaViewer, UmaViewer fork mode 0
    short    EyeBrow_1_R                   UmaViewer fork modes 1/2, and older forks
    unified  Brow_WaraiA_R                 UmaViewer fork mode 3 (descriptive, fits a vmd)

The unified spelling is what the fork exports by default: an english group token, the romaji action
tag and the side, without the numeric index or the source mesh. It exists because a vmd morph name
field holds only 15 bytes of shift-jis, so the tagged spelling can never be used to drive an exported
model, while the short spelling says nothing about what a morph does.

Operators ask for a shape key by (group, tag, side) and this module resolves the actual name by
trying every spelling, so all of them keep working. Optionally a ``naming_overrides.json`` next to
this addon can rename tags or force a preferred spelling.
"""

import json
import os
import re

GROUPS = ("EyeBrow", "Eye", "Ear", "Mouth")

#: group -> token used by the unified spelling (kept short: "EyeBrow_WaraiA_R" is 18 bytes)
GROUP_TOKENS = {
    "EyeBrow": "Brow",
    "Eye": "Eye",
    "Ear": "Ear",
    "Mouth": "Mouth",
}
TOKEN_TO_GROUP = {token: group for group, token in GROUP_TOKENS.items()}

#: tags that do not fit the 15 byte vmd limit together with their group
TAG_ABBREVIATIONS = {
    "EyelidHideA": "LidHideA",
    "EyelidHideB": "LidHideB",
}

_UNIFIED_RE = re.compile(r"^(?P<token>%s)_(?P<rest>.+)$" % "|".join(GROUP_TOKENS.values()))
_SHORT_RE = re.compile(r"^(?P<group>%s)_(?P<index>\d+)(?:_(?P<side>[LR]))$" % "|".join(GROUPS))
_TAGGED_RE = re.compile(
    r"^(?P<group>%s)_(?P<index>\d+)(?:_\d+)?(?:_(?P<side>[LR]))?\((?P<tag>[^)]*)\)(?:\[[^\]]*\])?$"
    % "|".join(GROUPS)
)

_overrides = None


def _load_overrides():
    """Optional ``naming_overrides.json`` beside this addon, e.g. {"convention": "unified", "tags": {...}}."""
    global _overrides
    if _overrides is not None:
        return _overrides
    _overrides = {}
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "naming_overrides.json")
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as handle:
                _overrides = json.load(handle) or {}
    except Exception as exc:  # noqa: BLE001 - a broken override file must not break the addon
        print(f"[uma_addon.naming] ignoring {path}: {exc}")
        _overrides = {}
    for tag, replacement in (_overrides.get("tags") or {}).items():
        TAG_ABBREVIATIONS[tag] = replacement
    return _overrides


def unified_name(group, tag, side=None):
    """``("EyeBrow", "WaraiA", "R")`` -> ``"Brow_WaraiA_R"``."""
    token = GROUP_TOKENS.get(group, group)
    tag = TAG_ABBREVIATIONS.get(tag, tag)
    return f"{token}_{tag}_{side}" if side else f"{token}_{tag}"


def tagged_name(group, index, tag, side=None, mesh=None):
    """The stock UmaViewer spelling, e.g. ``"EyeBrow_1_R(WaraiA)[M_Face]"``."""
    index_part = f"_{index}" if index is not None else ""
    side_part = f"_{side}" if side else ""
    return f"{group}{index_part}{side_part}({tag})[{mesh}]" if mesh else f"{group}{index_part}{side_part}({tag})"


def short_name(group, index, side=None):
    """``("EyeBrow", 1, "R")`` -> ``"EyeBrow_1_R"``."""
    index_part = f"_{index}" if index is not None else ""
    side_part = f"_{side}" if side else ""
    return f"{group}{index_part}{side_part}"


def parse(name):
    """Split any of the three spellings into ``{"group", "index", "side", "tag", "spelling"}``."""
    if not name:
        return None

    match = _TAGGED_RE.match(name)
    if match:
        return {"group": match.group("group"), "index": int(match.group("index")),
                "side": match.group("side"), "tag": match.group("tag"), "spelling": "tagged"}

    match = _UNIFIED_RE.match(name)
    if match:
        group = TOKEN_TO_GROUP[match.group("token")]
        rest = match.group("rest")
        side = None
        if len(rest) > 2 and rest[-2] == "_" and rest[-1] in "LR":
            side, rest = rest[-1], rest[:-2]
        tag = rest
        for original, abbreviated in TAG_ABBREVIATIONS.items():
            if tag == abbreviated:
                tag = original
                break
        return {"group": group, "index": None, "side": side, "tag": tag, "spelling": "unified"}

    match = _SHORT_RE.match(name)
    if match:
        return {"group": match.group("group"), "index": int(match.group("index")),
                "side": match.group("side"), "tag": None, "spelling": "short"}

    return None


def convention(shape_keys):
    """Which spelling a set of shape keys mostly uses: "unified", "tagged", "short" or None."""
    scores = {"unified": 0, "tagged": 0, "short": 0}
    for key in shape_keys:
        parsed = parse(key if isinstance(key, str) else key.name)
        if parsed:
            scores[parsed["spelling"]] += 1
    best = max(scores, key=lambda name: scores[name])
    return best if scores[best] else None


def _tag_matches(parsed, wanted_group, wanted_tag, wanted_side):
    """Match a key against a lookup, tolerating tags that embed their side ("CheekA_L")."""
    if parsed["group"] != wanted_group:
        return False
    if wanted_side and parsed["side"] and parsed["side"] != wanted_side:
        return False
    if wanted_side and not parsed["side"]:
        return False
    if wanted_tag is None:
        return True
    if parsed["tag"] in (wanted_tag, f"{wanted_tag}_{wanted_side}" if wanted_side else wanted_tag):
        return True
    if wanted_side and parsed["tag"] == wanted_tag:
        return True
    return False


def find(shape_keys, group, tag=None, side=None, index=None, mesh=None):
    """The name of the shape key describing ``(group, tag, side)``, or None.

    Every spelling is tried, so a model exported by any UmaViewer version resolves.
    """
    keys = [key if isinstance(key, str) else key.name for key in shape_keys]
    known = set(keys)

    for candidate in (unified_name(group, tag, side) if tag else None,
                      tagged_name(group, index, tag, side, mesh) if tag and index is not None else None,
                      short_name(group, index, side) if index is not None else None):
        if candidate and candidate in known:
            return candidate

    # last resort: any key that parses to the same group/tag/side, whatever its spelling
    if tag is not None:
        for key in keys:
            parsed = parse(key)
            if parsed and _tag_matches(parsed, group, tag, side):
                return key
    elif index is not None:
        for key in keys:
            parsed = parse(key)
            if parsed and parsed["group"] == group and parsed["index"] == index and parsed["side"] == side:
                return key
    return None


def target_name(shape_keys, group, tag, side=None, index=None, mesh=None):
    """The name a *new* shape key should get, following whatever the mesh already uses."""
    forced = (_load_overrides().get("convention") or "").strip().lower()
    spelling = forced or convention(shape_keys) or "unified"
    if spelling == "tagged":
        return tagged_name(group, index, tag, side, mesh)
    if spelling == "short":
        return short_name(group, index, side)
    return unified_name(group, tag, side)


def pairs(shape_keys, group, tag, index=None, mesh=None):
    """Both sides of a morph as ``(left, right)``, either of which may be None."""
    return (find(shape_keys, group, tag, "L", index, mesh),
            find(shape_keys, group, tag, "R", index, mesh))

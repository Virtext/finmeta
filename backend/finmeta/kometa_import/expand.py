"""Expand Kometa overlay files into a flat list of overlays.

Kometa builds overlays from templates with variables and conditionals. This module
evaluates that machinery (a documented subset of it) and returns one FlatOverlay per
entry under ``overlays:``, plus an ImportReport describing what could not be translated.

Semantics implemented (subset of Kometa):

* Template reserved keys ``default``, ``optional``, ``conditionals`` (and
  ``move_prefix``) produce no attributes. Everything else in a template is its body.
* ``default`` only supplies default values for ``<<variables>>``.
* ``conditionals.<var>`` sets ``<var>`` from the first condition whose criteria all
  match, else from the conditional's own ``default``; with neither, ``<var>`` stays unset.
  Criteria: ``<var>: value`` or ``<var>: [values]`` (equality, compared as strings),
  ``<var>.exists: true|false``. Criteria keys may themselves contain ``<<vars>>``.
* Variable precedence (highest first): the overlay's own ``variables`` and built-ins
  (``overlay_name``) > template conditionals > template ``default`` >
  ``external_templates.template_variables`` conditionals > its ``default``.
* ``<<var>>`` substitution, including nested names such as ``<<use_<<type>>>>``.
  A string that is exactly one ``<<var>>`` keeps the variable's type (int, float, list).
* An attribute (or list item) whose value is an unresolved variable listed in
  ``optional`` is dropped. An unresolved non-optional variable drops the attribute too,
  and the overlay is reported as ``partial`` with the variable name.
* Several templates per overlay (``template: [name: a, name: b]``) are merged in order;
  an earlier template's value wins over a later one for the same attribute.
* A template that is not defined in the file (e.g. ``standard`` from Kometa's built-in
  defaults) is skipped and reported; expansion continues with the templates that exist.
* Kometa runtime placeholders (``<<audience_rating>>`` and friends) are kept literally.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple, Union

import yaml

from .report import ImportReport, ReportItem

RESERVED_TEMPLATE_KEYS = {"default", "optional", "conditionals", "move_prefix"}

# Variables Kometa fills in at render time from the item itself; never "unresolved".
RUNTIME_VARS = {
    "audience_rating", "critic_rating", "user_rating", "title", "show_title",
    "season_title", "episode_title", "runtime", "bitrate", "originally_available",
    "content_rating", "season_number", "episode_number", "episode_count",
    "versions", "total_runtime",
}

# Keys that only make sense against a Plex server; reported, never silently dropped.
PLEX_ONLY_KEYS = {"plex_all", "plex_search", "plex_collectionless", "builder_level",
                  "ignore_blank_results", "run_definition"}

LAYOUT_KEYS = (
    "horizontal_offset", "horizontal_align", "vertical_offset", "vertical_align",
    "back_width", "back_height", "back_align", "back_color", "back_radius",
    "back_padding", "back_line_color", "back_line_width",
    "font", "font_size", "font_color", "font_style", "stroke_width", "stroke_color",
)

_VAR_RE = re.compile(r"<<([^<>]+)>>")
_FULL_VAR_RE = re.compile(r"^<<([^<>]+)>>$")


@dataclass
class FlatOverlay:
    """One fully expanded Kometa overlay entry."""

    key: str                                   # the overlay's key under `overlays:`
    name: Optional[str] = None                 # overlay name (may be "text(...)")
    file: Optional[str] = None                 # image path as written in the Kometa file
    group: Optional[str] = None
    weight: Optional[int] = None
    layout: Dict[str, Any] = field(default_factory=dict)
    conditions: Dict[str, Any] = field(default_factory=dict)   # filters / regex / gte / resolution ...
    suppress: List[str] = field(default_factory=list)          # suppress_overlays, if any
    plex_only: Dict[str, Any] = field(default_factory=dict)    # Plex-specific keys, kept as data
    templates_used: List[str] = field(default_factory=list)
    variables: Dict[str, Any] = field(default_factory=dict)    # all resolved variables
    body: Dict[str, Any] = field(default_factory=dict)         # merged expanded body

    @property
    def is_text(self) -> bool:
        return isinstance(self.name, str) and self.name.startswith("text(")


# --------------------------------------------------------------------------- helpers

class _Ctx:
    """Collects unresolved variable names while substituting."""

    def __init__(self, variables: Mapping[str, Any], optional: Set[str]):
        self.vars = variables
        self.optional = optional
        self.unresolved: Set[str] = set()

    def _lookup(self, name: str) -> Tuple[bool, Any]:
        if name in self.vars and self.vars[name] is not None:
            return True, self.vars[name]
        return False, None

    def sub_str(self, s: str) -> Any:
        """Substitute <<vars>> in s. Returns a typed value for a whole-string variable.

        Returns _DROP when the string is (or contains) an unresolved variable.
        """
        current = s
        for _ in range(10):                       # nested names resolve inside-out
            m = _FULL_VAR_RE.match(current)
            if m:
                ok, val = self._lookup(m.group(1))
                if ok:
                    if isinstance(val, str) and _VAR_RE.search(val) and val != current:
                        current = val
                        continue
                    return val
            changed = False

            def repl(mm: "re.Match[str]") -> str:
                nonlocal changed
                ok, val = self._lookup(mm.group(1))
                if ok and not isinstance(val, (dict, list)):
                    changed = True
                    return str(val)
                return mm.group(0)

            new = _VAR_RE.sub(repl, current)
            if not changed:
                break
            current = new

        leftovers = [n for n in _VAR_RE.findall(current) if n not in RUNTIME_VARS]
        if leftovers:
            for n in leftovers:
                if n not in self.optional:
                    self.unresolved.add(n)
            return _DROP
        return current

    def sub(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.sub_str(value)
        if isinstance(value, list):
            out = []
            for item in value:
                v = self.sub(item)
                if v is not _DROP:
                    out.append(v)
            return out
        if isinstance(value, dict):
            out_d = {}
            for k, v in value.items():
                kk = self.sub_str(k) if isinstance(k, str) else k
                if kk is _DROP:
                    continue
                vv = self.sub(v)
                if vv is _DROP:
                    continue
                out_d[kk] = vv
            return out_d
        return value


class _Drop:
    def __repr__(self) -> str:   # pragma: no cover
        return "<DROP>"


_DROP = _Drop()


def _as_list(v: Any) -> List[Any]:
    return v if isinstance(v, list) else [v]


def _criteria_match(cond: Mapping[str, Any], lookup: Mapping[str, Any]) -> bool:
    ctx = _Ctx(lookup, set())
    for raw_k, want in cond.items():
        if raw_k == "value":
            continue
        k = ctx.sub_str(raw_k) if isinstance(raw_k, str) else raw_k
        if k is _DROP:
            return False
        k = str(k)
        if k.endswith(".exists"):
            name = k[: -len(".exists")]
            present = lookup.get(name) is not None
            if present != bool(want):
                return False
            continue
        if k.endswith(".not"):
            name = k[: -len(".not")]
            have = lookup.get(name)
            if have is not None and str(have) in {str(w) for w in _as_list(want)}:
                return False
            continue
        have = lookup.get(k)
        if have is None:
            return False
        if str(have) not in {str(w) for w in _as_list(want)}:
            return False
    return True


def _eval_conditionals(conditionals: Mapping[str, Any], lookup: Mapping[str, Any]) -> Dict[str, Any]:
    """Evaluate a conditionals block against lookup; returns {var: raw value}."""
    out: Dict[str, Any] = {}
    for var, spec in (conditionals or {}).items():
        if not isinstance(spec, Mapping):
            continue
        chosen: Any = None
        found = False
        for cond in spec.get("conditions") or []:
            if isinstance(cond, Mapping) and "value" in cond and _criteria_match(cond, lookup):
                chosen, found = cond["value"], True
                break
        if not found and "default" in spec:
            chosen, found = spec["default"], True
        if found:
            out[str(var)] = chosen
    return out


def _deep_merge_keep_first(a: Dict[str, Any], b: Mapping[str, Any]) -> Dict[str, Any]:
    """Merge b into a; existing values in a win, nested dicts merge."""
    for k, v in b.items():
        if k not in a:
            a[k] = v
        elif isinstance(a[k], dict) and isinstance(v, Mapping):
            _deep_merge_keep_first(a[k], v)
    return a


def _template_names(entry: Mapping[str, Any]) -> List[str]:
    t = entry.get("template")
    names: List[str] = []
    for item in _as_list(t) if t is not None else []:
        if isinstance(item, Mapping) and "name" in item:
            names.append(str(item["name"]))
        elif isinstance(item, str):
            names.append(item)
    return names


# --------------------------------------------------------------------------- core

def _resolve_variables(
    overlay_key: str,
    overlay_vars: Mapping[str, Any],
    template: Mapping[str, Any],
    tv: Mapping[str, Any],
) -> Dict[str, Any]:
    """Build the variable set for one template call, following the precedence rules."""
    base: Dict[str, Any] = {"overlay_name": overlay_key}
    base.update({str(k): v for k, v in (overlay_vars or {}).items()})

    tpl_default = dict(template.get("default") or {})
    tv_default = dict(tv.get("default") or {})

    # Criteria may look at defaults too (overlay variables still win).
    lookup = {**tv_default, **tpl_default, **base}

    final = dict(base)
    layers = [
        _eval_conditionals(template.get("conditionals") or {}, lookup),
        tpl_default,
        _eval_conditionals(tv.get("conditionals") or {}, lookup),
        tv_default,
    ]
    for layer in layers:
        for k, v in layer.items():
            if final.get(k) is None:
                final[k] = v

    # Values may reference other variables (e.g. ".../<<key>>.png"); resolve them.
    ctx = _Ctx(final, set())
    for k in list(final.keys()):
        v = final[k]
        if isinstance(v, (str, list, dict)):
            r = ctx.sub(v)
            final[k] = None if r is _DROP else r
    return final


def expand_data(data: Mapping[str, Any], source: str = "<memory>") -> Tuple[List[FlatOverlay], ImportReport]:
    report = ImportReport(source=source)
    templates: Mapping[str, Any] = data.get("templates") or {}
    tv: Mapping[str, Any] = (data.get("external_templates") or {}).get("template_variables") or {}
    overlays: Mapping[str, Any] = data.get("overlays") or {}

    flat: List[FlatOverlay] = []
    for raw_key, entry in overlays.items():
        key = str(raw_key)
        item = ReportItem(key=key)
        if not isinstance(entry, Mapping):
            item.status = "skipped"
            item.notes.append("overlay entry is not a mapping")
            report.items.append(item)
            continue

        overlay_vars = entry.get("variables") or {}
        names = _template_names(entry)
        merged_body: Dict[str, Any] = {}
        merged_vars: Dict[str, Any] = {}
        used: List[str] = []
        unresolved: Set[str] = set()

        # The entry's own body (anything besides variables/template) comes first.
        own_body = {k: v for k, v in entry.items() if k not in ("variables", "template")}

        for tname in names:
            template = templates.get(tname)
            if not isinstance(template, Mapping):
                item.notes.append(f"template '{tname}' is not defined in this file (Kometa default?) — skipped")
                report.missing_templates.add(tname)
                continue
            used.append(tname)
            variables = _resolve_variables(key, overlay_vars, template, tv)
            optional_ctx = _Ctx(variables, set())
            optional = set()
            for o in template.get("optional") or []:
                r = optional_ctx.sub_str(str(o))
                optional.add(str(o) if r is _DROP else str(r))
            body = {k: v for k, v in template.items() if k not in RESERVED_TEMPLATE_KEYS}
            ctx = _Ctx(variables, optional)
            expanded = ctx.sub(body)
            unresolved |= ctx.unresolved
            _deep_merge_keep_first(merged_body, expanded)
            for k, v in variables.items():
                if merged_vars.get(k) is None and v is not None:
                    merged_vars[k] = v

        if own_body:
            ctx = _Ctx(merged_vars or {"overlay_name": key, **overlay_vars}, set())
            own = ctx.sub(own_body)
            unresolved |= ctx.unresolved
            merged = dict(own)
            _deep_merge_keep_first(merged, merged_body)
            merged_body = merged

        if not used and not own_body:
            item.status = "skipped"
            item.notes.append("no usable template or body")
            report.items.append(item)
            continue

        if not merged_vars:
            merged_vars = {"overlay_name": key, **{str(k): v for k, v in overlay_vars.items()}}

        fo = _build_flat(key, merged_body, merged_vars, used)
        flat.append(fo)

        if fo.plex_only:
            item.notes.append("Plex-only keys kept as data: " + ", ".join(sorted(fo.plex_only)))
        if unresolved:
            item.notes.append("unresolved variables: " + ", ".join(sorted(unresolved)))
        if report.missing_templates.intersection(names) or unresolved:
            item.status = "partial"
        else:
            item.status = "ok"
        report.items.append(item)

    return flat, report


def _build_flat(key: str, body: Dict[str, Any], variables: Dict[str, Any], used: List[str]) -> FlatOverlay:
    ov = body.get("overlay") if isinstance(body.get("overlay"), Mapping) else {}

    def pick(name: str) -> Any:
        if ov.get(name) is not None:
            return ov[name]
        return variables.get(name)

    weight = pick("weight")
    try:
        weight = int(weight) if weight is not None else None
    except (TypeError, ValueError):
        weight = None

    layout = {k: pick(k) for k in LAYOUT_KEYS if pick(k) is not None}

    conditions: Dict[str, Any] = {}
    filters = body.get("filters")
    if isinstance(filters, Mapping):
        conditions.update({str(k): v for k, v in filters.items()})
    ps = body.get("plex_search")
    if isinstance(ps, Mapping):
        for scope in ps.values():
            if isinstance(scope, Mapping):
                for k, v in scope.items():
                    conditions.setdefault(str(k), v)
    for k in ("allowed_libraries", "builder_level"):
        if variables.get(k) is not None:
            conditions.setdefault(k, variables[k])

    plex_only = {k: body[k] for k in PLEX_ONLY_KEYS if k in body}

    suppress = variables.get("suppress_overlays") or []
    suppress = [str(s) for s in _as_list(suppress)] if suppress else []

    name = pick("name")
    return FlatOverlay(
        key=key,
        name=str(name) if name is not None else key,
        file=pick("file"),
        group=pick("group"),
        weight=weight,
        layout=layout,
        conditions=conditions,
        suppress=suppress,
        plex_only=plex_only,
        templates_used=used,
        variables=variables,
        body=body,
    )


def expand_file(path: Union[str, Path]) -> Tuple[List[FlatOverlay], ImportReport]:
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, Mapping):
        rep = ImportReport(source=str(p))
        rep.errors.append("file is not a YAML mapping")
        return [], rep
    return expand_data(data, source=str(p))

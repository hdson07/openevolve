"""
Solver parameter catalog. Loads + validates the per-solver `params.json`
file shipped under `<bench>/evolve/params.json`.

The catalog is the single source of truth for:
  - which keys the LLM is allowed to tune (everything in `groups[*].params`)
  - default values that anchor the BASELINE (replaces baseline_params.BASELINE)
  - locked keys that must not deviate (replaces baseline_params.LOCKED)
  - per-key type + range + enum constraints for runtime validation
  - LLM-facing reference text rendered into `prompt.system_message`

Schema (loose validation — extra keys ignored, missing keys fall back to defaults):

    {
      "solver": "cpsat",
      "version": "<free text>",
      "defaults": {<param>: <value>},
      "locked":   {<param>: <value>},
      "groups": {
        "<group_name>": {
          "description": "...",
          "params": {
            "<param>": {
              "type": "int|float|bool|str|enum",
              "default": <value>,
              "range": [lo, hi],            // numeric only, optional
              "values": [...],              // enum only
              "desc": "..."
            }
          }
        }
      },
      "subsolver_names": ["..."]            // optional, solver-specific
    }
"""
import json
import pathlib


_NUMERIC_TYPES = {"int", "float"}
_VALID_TYPES = {"int", "float", "bool", "str", "enum", "list"}


class Catalog:
    def __init__(self, data, source):
        self._data = data
        self._source = source
        self._flat = {}
        for gname, group in (data.get("groups") or {}).items():
            for pname, spec in (group.get("params") or {}).items():
                self._flat[pname] = dict(spec, _group=gname)

    @property
    def solver(self):
        return self._data.get("solver")

    @property
    def version(self):
        return self._data.get("version", "")

    @property
    def defaults(self):
        return dict(self._data.get("defaults") or {})

    @property
    def locked(self):
        return dict(self._data.get("locked") or {})

    @property
    def subsolver_names(self):
        return list(self._data.get("subsolver_names") or [])

    def flat(self):
        return dict(self._flat)

    def known_keys(self):
        return set(self._flat.keys())

    def validate(self, params):
        """Return list of (key, error_message) tuples; empty list = ok.

        Catches the obvious LLM mistakes without spawning a solver subprocess:
        unknown key, wrong type, out-of-range value, unlisted enum value.
        Subsolver-name lists (e.g. extra_subsolvers) are checked element-wise
        against `subsolver_names` if defined.
        """
        errors = []
        known = self.known_keys()
        locked = self.locked
        defaults = self.defaults
        subsolvers = set(self.subsolver_names)
        for k, v in params.items():
            if k in locked:
                if v != locked[k]:
                    errors.append((k, f"locked key changed: expected {locked[k]!r}, got {v!r}"))
                continue
            if k in defaults and k not in known:
                continue
            spec = self._flat.get(k)
            if spec is None:
                errors.append((k, "unknown key (not in catalog)"))
                continue
            err = _check_spec(k, v, spec, subsolvers)
            if err:
                errors.append((k, err))
        return errors

    def render_prompt_section(self):
        """Render the catalog as a compact reference block for the LLM prompt.

        Used to replace the `{{params_reference}}` token in
        config.yaml `prompt.system_message`.
        """
        lines = [f"=== {self.solver} parameter catalog ({self.version}) ==="]
        locked = self.locked
        if locked:
            lines.append("LOCKED (must not modify): " + ", ".join(sorted(locked.keys())))
        for gname, group in (self._data.get("groups") or {}).items():
            desc = (group.get("description") or "").strip()
            lines.append("")
            lines.append(f"[{gname}] {desc}")
            for pname, spec in (group.get("params") or {}).items():
                lines.append("  " + _format_spec_line(pname, spec))
        if self.subsolver_names:
            lines.append("")
            lines.append("subsolver names: " + ", ".join(self.subsolver_names))
        return "\n".join(lines)


def _check_spec(key, value, spec, subsolvers):
    t = spec.get("type")
    if t not in _VALID_TYPES:
        return None  # spec malformed — don't fail the LLM for our mistake
    if t == "bool":
        if not isinstance(value, bool):
            return f"expected bool, got {type(value).__name__}"
        return None
    if t == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            return f"expected int, got {type(value).__name__}"
        return _check_range(value, spec)
    if t == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"expected float, got {type(value).__name__}"
        return _check_range(value, spec)
    if t == "str":
        if not isinstance(value, str):
            return f"expected str, got {type(value).__name__}"
        return None
    if t == "enum":
        values = spec.get("values") or []
        if value not in values:
            return f"value {value!r} not in enum {values!r}"
        return None
    if t == "list":
        if not isinstance(value, list):
            return f"expected list, got {type(value).__name__}"
        elem_t = spec.get("element_type")
        if elem_t == "subsolver" and subsolvers:
            bad = [x for x in value if x not in subsolvers]
            if bad:
                return f"unknown subsolver names: {bad!r}"
        return None
    return None


def _check_range(value, spec):
    rng = spec.get("range")
    if not rng or len(rng) != 2:
        return None
    lo, hi = rng
    if lo is not None and value < lo:
        return f"{value} below range min {lo}"
    if hi is not None and value > hi:
        return f"{value} above range max {hi}"
    return None


def _format_spec_line(name, spec):
    t = spec.get("type", "?")
    d = spec.get("default")
    parts = [f"{name}({t}"]
    if "range" in spec:
        lo, hi = spec["range"]
        parts.append(f"=[{lo}..{hi}]")
    elif "values" in spec:
        parts.append("=" + "|".join(repr(v) for v in spec["values"]))
    parts.append(f", default={d!r})")
    line = "".join(parts)
    desc = (spec.get("desc") or "").strip()
    if desc:
        line += " — " + desc
    return line


_cache = {}


def load(path):
    """Load a params.json file. Result cached by absolute path."""
    path = pathlib.Path(path).resolve()
    key = str(path)
    if key in _cache:
        return _cache[key]
    data = json.loads(path.read_text())
    cat = Catalog(data, path)
    _cache[key] = cat
    return cat


def load_for_bench(bench_root):
    """Convenience: load `<bench_root>/params.json`.

    `bench_root` is the absolute path to `input/<bench>/evolve/`.
    """
    return load(pathlib.Path(bench_root) / "params.json")

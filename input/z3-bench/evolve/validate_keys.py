"""
Validate phase EVOLVE-BLOCK keys against the installed z3 binary.

Strategy:
  1. `z3 -pm` -> list of module names
  2. `z3 -pm:<mod>` for each module -> option names + types + defaults
  3. `z3 -p` -> global (module-less) option names
  -> build canonical set of valid `module.option` (and bare global) keys
  Then for each phase initial_program.py, report invalid keys.

Run inside the container (z3 binary required):

    python input/z3-bench/evolve/validate_keys.py

Output:
    shared/z3_valid_keys.json     # full valid set
    stdout report per phase, listing invalid keys

Does NOT modify initial_program.py. Edit manually after reviewing.
"""
import importlib.util
import json
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent
SHARED = ROOT / "shared"
PHASES = [
    "phase1_opt_sls",
    "phase2_sat",
    "phase3_smt",
    "phase4_unified",
]


_OPTION_LINE_RE = re.compile(r"^\s+([\w\-]+(?:\.[\w\-]+)*)\s+\(")


def _run_z3(args):
    proc = subprocess.run(["z3", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        # Some -pm:<mod> invocations exit non-zero on unknown module; treat as soft.
        return proc.stdout, proc.stderr, proc.returncode
    return proc.stdout, proc.stderr, 0


def _parse_options(text):
    """Pull '   name  (type) ...' lines out of any z3 doc dump."""
    names = []
    for line in text.splitlines():
        m = _OPTION_LINE_RE.match(line)
        if m:
            names.append(m.group(1))
    return names


# Z3 4.13.x format: `[module] <name>(, description: <prose>)?`
# Older builds: `[module] <name>` alone. Take only the first identifier token.
_MODULE_HEADER_RE = re.compile(r"^\[module\]\s+([A-Za-z_][\w\-]*)")


def _list_modules():
    stdout, stderr, rc = _run_z3(["-pm"])
    if rc != 0:
        print(f"`z3 -pm` failed: {stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    mods = set()
    for line in stdout.splitlines():
        m = _MODULE_HEADER_RE.match(line)
        if m:
            mods.add(m.group(1))
    if not mods:
        print("warning: no `[module] <name>` headers found in `z3 -pm` output", file=sys.stderr)
    return sorted(mods)


def get_valid_keys():
    if shutil.which("z3") is None:
        print("ERROR: z3 binary not on PATH. Install in container first.", file=sys.stderr)
        sys.exit(1)

    valid = set()

    # 1. Globals via `z3 -p`
    stdout, _, rc = _run_z3(["-p"])
    if rc == 0:
        for opt in _parse_options(stdout):
            valid.add(opt)

    # 2. Modules + their options. Try each candidate token from `-pm` output;
    # silently skip ones z3 rejects (prose/noise gets filtered this way).
    mods = _list_modules()
    if not mods:
        print("warning: no modules parsed from `z3 -pm`", file=sys.stderr)
    tried = 0
    accepted = 0
    debug_dumped = False
    for mod in mods:
        tried += 1
        stdout, stderr, rc = _run_z3([f"-pm:{mod}"])
        if rc != 0:
            continue   # quiet skip
        accepted += 1
        opts = _parse_options(stdout)
        if not opts and not debug_dumped:
            print(f"  no options parsed from module {mod!r}; raw output (first 30 lines):", file=sys.stderr)
            for line in stdout.splitlines()[:30]:
                print(f"    | {line}", file=sys.stderr)
            debug_dumped = True
        for opt in opts:
            valid.add(f"{mod}.{opt}")
    print(f"  modules: {accepted}/{tried} accepted, {len(valid)} valid keys total")

    return valid


def load_program_params(phase_dir):
    """Load initial_program.py and return its get_params() output."""
    sys.path.insert(0, str(SHARED))
    spec = importlib.util.spec_from_file_location(
        f"prog_{phase_dir.name}", phase_dir / "initial_program.py"
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.get_params()


def main():
    print("Probing z3 -p / z3 -pm / z3 -pm:<module> ...")
    valid = get_valid_keys()
    out = SHARED / "z3_valid_keys.json"
    out.write_text(json.dumps(sorted(valid), indent=2))
    print(f"wrote {out.relative_to(ROOT)} ({len(valid)} entries)\n")

    any_invalid = False
    for ph in PHASES:
        d = ROOT / ph
        if not (d / "initial_program.py").exists():
            continue
        params = load_program_params(d)
        invalid = sorted(k for k in params if k not in valid)
        status = "OK" if not invalid else f"INVALID x{len(invalid)}"
        print(f"=== {ph}: {len(params)} keys, {status} ===")
        for k in invalid:
            print(f"  -  {k!r}")
        print()
        if invalid:
            any_invalid = True

    if any_invalid:
        print("Edit the offending initial_program.py files to remove or rename invalid keys.")
        sys.exit(2)
    print("All phase keys validate against `z3 -pm` introspection.")


if __name__ == "__main__":
    main()

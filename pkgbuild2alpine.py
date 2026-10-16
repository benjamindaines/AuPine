#!/usr/bin/env python3
"""Convert an Arch Linux PKGBUILD into an Alpine Linux install script.

The PKGBUILD is parsed statically and is never executed. Dependencies are mapped
to Alpine package names; the generated script verifies availability against the
configured apk repositories at run time. Packages found in the repositories are
installed with apk. Git sources and unresolved dependencies are not cloned
automatically: the generated script prompts the user to clone them and then
continues with the build and install steps.

Usage:
    pkgbuild2alpine.py PKGBUILD [-o install-NAME.sh] [--map EXTRA.map]

Map file format (one entry per line, '#' starts a comment):
    arch-package-name  alpine-candidate-1  [alpine-candidate-2 ...]
"""

from __future__ import annotations

import argparse
import re
import shlex
import stat
import sys
from pathlib import Path

# Arch package name -> ordered Alpine candidates (first available package wins).
ARCH_TO_ALPINE: dict[str, list[str]] = {
    "base-devel": ["build-base"],
    "awk": ["gawk"],
    "linux-api-headers": ["linux-headers"],
    "pkgconf": ["pkgconf"],
    "pkg-config": ["pkgconf"],
    "ninja": ["samurai"],
    "freetype2": ["freetype"],
    "glib2": ["glib"],
    "gtk3": ["gtk+3.0"],
    "gtk2": ["gtk+2.0"],
    "qt5-base": ["qt5-qtbase"],
    "qt6-base": ["qt6-qtbase"],
    "sdl2": ["sdl2"],
    "python": ["python3"],
    "python-pip": ["py3-pip"],
    "python-build": ["py3-build"],
    "python-installer": ["py3-installer"],
    "python-setuptools": ["py3-setuptools"],
    "python-setuptools-scm": ["py3-setuptools_scm"],
    "python-wheel": ["py3-wheel"],
    "python-yaml": ["py3-yaml"],
    "python-pyyaml": ["py3-yaml"],
    "python-dateutil": ["py3-dateutil"],
    "python-pillow": ["py3-pillow"],
    "python-gobject": ["py3-gobject3"],
    "python-poetry-core": ["py3-poetry-core"],
    "python-flit-core": ["py3-flit-core"],
    "python-hatchling": ["py3-hatchling"],
    "nodejs": ["nodejs"],
    "rust": ["rust", "cargo"],
    "valkey": ["valkey"],
    "redis": ["valkey", "redis"],
}

# Packages assumed present on the target or without an Alpine counterpart.
IGNORED = {"glibc", "gcc-libs", "filesystem", "systemd", "systemd-libs"}

# Variables that are not re-emitted as plain shell assignments.
SKIP_VARS = {"source", "arch", "license", "depends", "makedepends", "checkdepends",
             "optdepends", "provides", "conflicts", "replaces", "backup",
             "options", "install", "changelog", "noextract", "validpgpkeys"}

FUNC_RE = re.compile(r"^(\w+)\s*\(\)\s*\{", re.M)


def strip_comment(line: str) -> str:
    """Remove a shell comment that starts at a word boundary."""
    return re.sub(r"(^|\s)#.*$", r"\1", line)


def expand(token: str, env: dict) -> str:
    """Expand $name and ${name} references that are defined in env."""
    def sub(m: re.Match) -> str:
        val = env.get(m.group(1) or m.group(2))
        if isinstance(val, list):
            val = val[0] if val else ""
        return m.group(0) if val is None else val
    return re.sub(r"\$\{(\w+)\}|\$(\w+)", sub, token)


def split_functions(text: str) -> tuple[dict[str, str], str]:
    """Separate shell function definitions from top-level text (brace matching)."""
    funcs: dict[str, str] = {}
    rest: list[str] = []
    pos = 0
    for m in FUNC_RE.finditer(text):
        if m.start() < pos:
            continue
        depth, i = 0, m.end() - 1
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        funcs[m.group(1)] = text[m.start():i + 1]
        rest.append(text[pos:m.start()])
        pos = i + 1
    rest.append(text[pos:])
    return funcs, "".join(rest)


def parse_vars(text: str) -> dict[str, str | list[str]]:
    """Collect top-level scalar and array assignments."""
    env: dict[str, str | list[str]] = {}
    lines = [strip_comment(line) for line in text.splitlines()]
    i = 0
    while i < len(lines):
        m = re.match(r"^\s*(\w+)=(.*)$", lines[i])
        i += 1
        if not m:
            continue
        name, val = m.group(1), m.group(2).strip()
        if val.startswith("("):
            while not val.endswith(")") and i < len(lines):
                val += "\n" + lines[i].strip()
                i += 1
            env[name] = [expand(t, env) for t in shlex.split(val[1:-1])]
        else:
            toks = shlex.split(val)
            env[name] = expand(toks[0], env) if toks else ""
    return env


def as_list(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def dep_name(dep: str) -> str:
    """Strip version constraints and optdepends descriptions."""
    return re.split(r"[<>=:]", dep, maxsplit=1)[0].strip()


def load_extra_map(path: str | None) -> dict[str, list[str]]:
    extra: dict[str, list[str]] = {}
    if not path:
        return extra
    for raw in Path(path).read_text().splitlines():
        parts = strip_comment(raw).split()
        if len(parts) >= 2:
            extra[parts[0]] = parts[1:]
    return extra


def candidates(name: str, table: dict[str, list[str]]) -> list[str] | None:
    """Return Alpine candidates for an Arch package, or None when ignored."""
    if name in IGNORED:
        return None
    if name in table:
        return table[name]
    cands: list[str] = []
    if name.startswith("python-"):
        cands.append("py3-" + name[len("python-"):])
    cands.append(name)
    if name.endswith("-libs"):
        cands.append(name[: -len("-libs")])
    return cands


def dep_lines(deps: list[str], table: dict[str, list[str]]) -> str:
    out: list[str] = []
    seen: set[str] = set()
    for dep in deps:
        name = dep_name(dep)
        if not name or name in seen:
            continue
        seen.add(name)
        cands = candidates(name, table)
        if cands is None:
            out.append(f"  # ignored: {name}")
        else:
            out.append("  " + shlex.quote(f"{name}|{' '.join(cands)}"))
    return "\n".join(out)


def classify_source(entry: str) -> str:
    """Encode a PKGBUILD source entry as a pipe-separated record."""
    name = ""
    if "::" in entry:
        name, entry = entry.split("::", 1)
    m = re.match(r"^git\+(.+?)(?:#(\w+)=(.+))?$", entry)
    if m:
        url, kind, ref = m.group(1), m.group(2) or "", m.group(3) or ""
        base = name or re.sub(r"\.git$", "", url.rstrip("/").rsplit("/", 1)[-1])
        return f"git|{base}|{url}|{kind}|{ref}"
    if re.match(r"^\w+://", entry):
        return f"url|{name or entry.rsplit('/', 1)[-1]}|{entry}"
    return f"file|{entry}"


def var_lines(env: dict[str, str | list[str]]) -> str:
    out: list[str] = []
    for name, val in env.items():
        if name in SKIP_VARS or name.endswith("sums"):
            continue
        scalar = as_list(val)[0] if as_list(val) else ""
        out.append(f"{name}={shlex.quote(scalar)}")
    return "\n".join(out)


TEMPLATE = r"""#!/usr/bin/env bash
# Generated by pkgbuild2alpine.py from the PKGBUILD of @@PKGNAME@@.
# Target: Alpine Linux (apk) with bash, GNU coreutils and glibc compatibility present.
# Run as root: ./@@SCRIPT@@
# Environment overrides: SRCDIR (source/work directory), PKGDIR (staging directory).
@@NOTES@@
set -euo pipefail

# Variables carried over from the PKGBUILD.
@@VARS@@

srcdir=${SRCDIR:-$PWD/src}
pkgdir=${PKGDIR:-$PWD/pkg}
PKGBUILD_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
INVOKER=${SUDO_USER:-${DOAS_USER:-}}
export srcdir pkgdir

# Repositories owned by the invoking user are trusted for this process only.
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0='*'

# Entries: "<arch-name>|<alpine candidates, first available wins>".
RUNTIME_DEPS=(
@@RUNTIME_DEPS@@
)
BUILD_DEPS=(
@@BUILD_DEPS@@
)

# Entries: "git|dir|url|kind|ref", "url|filename|url" or "file|name".
SOURCES=(
@@SOURCES@@
)

# Original PKGBUILD functions (unmodified).
@@FUNCTIONS@@

msg()  { printf '\n==> %s\n' "$*"; }
warn() { printf '\n==> WARNING: %s\n' "$*" >&2; }
die()  { printf '\n==> ERROR: %s\n' "$*" >&2; exit 1; }

ask() {
  local reply
  read -r -p "$1" reply </dev/tty || die "an interactive terminal is required."
  printf '%s' "$reply"
}

[ "$(id -u)" -eq 0 ] || die "root privileges are required (doas or sudo)."
command -v apk >/dev/null 2>&1 || die "apk not found; Alpine Linux is required."

apk_has() { [ -n "$(apk search -e "$1" 2>/dev/null)" ]; }

APK_PKGS=(bash git curl tar)
UNRESOLVED=()

# resolve_deps <with-dev:0|1> entries...
resolve_deps() {
  local with_dev=$1 entry arch cands c found
  shift
  for entry in "$@"; do
    arch=${entry%%|*}
    cands=${entry#*|}
    found=
    for c in $cands; do
      if apk_has "$c"; then found=$c; break; fi
    done
    if [ -z "$found" ]; then
      case " ${UNRESOLVED[*]:-} " in *" $arch "*) ;; *) UNRESOLVED+=("$arch") ;; esac
      continue
    fi
    APK_PKGS+=("$found")
    if [ "$with_dev" = 1 ] && apk_has "$found-dev"; then APK_PKGS+=("$found-dev"); fi
  done
}

need_tool() { command -v "$1" >/dev/null 2>&1 || apk add "$2"; }

# Builds and installs an upstream source tree using the first recognised build system.
install_source_tree() (
  cd "$1"
  if [ -f meson.build ]; then
    need_tool meson meson
    meson setup build --prefix=/usr && meson compile -C build && meson install -C build
  elif [ -f CMakeLists.txt ]; then
    need_tool cmake cmake
    cmake -B build -DCMAKE_INSTALL_PREFIX=/usr -DCMAKE_BUILD_TYPE=Release \
      && cmake --build build && cmake --install build
  elif [ -f Cargo.toml ]; then
    need_tool cargo cargo
    cargo install --path . --root /usr/local
  elif [ -f configure ]; then
    ./configure --prefix=/usr && make && make install
  elif [ -f pyproject.toml ] || [ -f setup.py ]; then
    need_tool pip py3-pip
    pip install --break-system-packages .
  elif [ -f Makefile ]; then
    make && make install
  else
    die "no recognised build system in $1; build it manually, then rerun."
  fi
)

# Prompts the user to clone a dependency that is absent from the apk repositories.
install_dep_from_source() {
  local name=$1 dir="$srcdir/deps/$1" ans
  mkdir -p "$srcdir/deps"
  [ -n "$INVOKER" ] && chown "$INVOKER" "$srcdir/deps"
  while [ ! -d "$dir" ]; do
    printf "\nDependency '%s' is not available in the apk repositories.\n" "$name"
    printf 'Clone the upstream repository as the regular user (not root):\n\n'
    printf '    git clone <upstream-url> %s\n\n' "$dir"
    ans=$(ask "Press Enter after cloning, or type 'skip' to continue without it: ")
    if [ "$ans" = skip ]; then warn "'$name' skipped."; return 0; fi
    [ -d "$dir" ] || warn "directory not found: $dir"
  done
  msg "Building '$name' from source"
  install_source_tree "$dir"
}

# Prompts the user to clone a git source listed in the PKGBUILD.
prompt_clone() {
  local name=$1 url=$2 kind=$3 ref=$4 dir="$srcdir/$1" cmd ans
  if [ -d "$dir/.git" ]; then
    msg "Using existing clone: $dir"
    return 0
  fi
  cmd="git clone $url $dir"
  case $kind in
    branch|tag) cmd="git clone --branch $ref $url $dir" ;;
    commit) cmd="$cmd && git -C $dir checkout $ref" ;;
  esac
  while [ ! -d "$dir/.git" ]; do
    printf '\nSource repository required: %s\n' "$url"
    printf 'Clone it as the regular user (not root), for example:\n\n'
    printf '    %s\n\n' "$cmd"
    ans=$(ask "Press Enter after cloning: ")
    [ -d "$dir/.git" ] || warn "no git repository found at $dir"
  done
}

fetch_sources() {
  local entry kind a b c d
  for entry in "${SOURCES[@]}"; do
    IFS='|' read -r kind a b c d <<<"$entry"
    case $kind in
      git) prompt_clone "$a" "$b" "$c" "$d" ;;
      url)
        if [ ! -e "$srcdir/$a" ]; then
          msg "Downloading $b"
          curl -fL -o "$srcdir/$a" "$b"
        fi
        case $a in *.tar|*.tar.*|*.tgz) tar -xf "$srcdir/$a" -C "$srcdir" ;; esac
        ;;
      file)
        if [ -e "$PKGBUILD_DIR/$a" ]; then
          cp -a "$PKGBUILD_DIR/$a" "$srcdir/$a"
        else
          while [ ! -e "$srcdir/$a" ]; do
            printf '\nLocal source file required: %s\n' "$a"
            printf 'Place it in %s (or next to this script).\n' "$srcdir"
            ask "Press Enter once the file is in place: " >/dev/null
          done
        fi
        ;;
    esac
  done
}

run_phase() {
  declare -F "$1" >/dev/null || return 0
  msg "Running $1()"
  ( set -euo pipefail; cd "$srcdir"; "$1" )
}

# Minimal systemd-sysusers translation (g and u entries).
apply_sysusers() {
  local line type name gecos home shell
  while IFS= read -r line; do
    case $line in ''|'#'*) continue ;; esac
    set -f; eval "set -- $line"; set +f
    type=$1; name=$2; gecos=${4:--}; home=${5:--}; shell=${6:--}
    [ "$home" = - ] && home=/
    [ "$shell" = - ] && shell=/sbin/nologin
    [ "$gecos" = - ] && gecos=$name
    case $type in
      g) getent group "$name" >/dev/null || addgroup -S "$name" ;;
      u)
        getent group "$name" >/dev/null || addgroup -S "$name"
        getent passwd "$name" >/dev/null \
          || adduser -S -D -H -h "$home" -s "$shell" -g "$gecos" -G "$name" "$name"
        ;;
    esac
  done <"$1"
}

# Minimal systemd-tmpfiles translation (d and D entries).
apply_tmpfiles() {
  local line mode owner group
  while IFS= read -r line; do
    case $line in ''|'#'*) continue ;; esac
    set -f; eval "set -- $line"; set +f
    case $1 in d|D|d!|D!) ;; *) continue ;; esac
    mkdir -p "$2"
    mode=${3:--}; owner=${4:--}; group=${5:--}
    if [ "$mode" != - ]; then chmod "$mode" "$2"; fi
    if [ "$owner" != - ]; then chown "$owner" "$2"; fi
    if [ "$group" != - ]; then chgrp "$group" "$2"; fi
  done <"$1"
}

msg "Updating the apk package index"
apk update
resolve_deps 0 "${RUNTIME_DEPS[@]}"
resolve_deps 1 "${BUILD_DEPS[@]}"
msg "Installing packages from the apk repositories"
apk add "${APK_PKGS[@]}"

mkdir -p "$srcdir"
if [ -n "$INVOKER" ]; then chown "$INVOKER" "$srcdir"; fi

for dep in "${UNRESOLVED[@]}"; do
  install_dep_from_source "$dep"
done

fetch_sources

if declare -F pkgver >/dev/null; then
  pkgver=$(cd "$srcdir" && pkgver)
  msg "Version: $pkgver"
fi
rm -rf "$pkgdir"
mkdir -p "$pkgdir"
@@PHASES@@

msg "Installing staged files into /"
cp -a "$pkgdir"/. /

for f in "$pkgdir"/usr/lib/sysusers.d/*.conf; do
  if [ -e "$f" ]; then apply_sysusers "$f"; fi
done
for f in "$pkgdir"/usr/lib/tmpfiles.d/*.conf; do
  if [ -e "$f" ]; then apply_tmpfiles "$f"; fi
done
if [ -d "$pkgdir/usr/lib/systemd" ]; then
  warn "systemd units were installed but Alpine uses OpenRC; a service script"
  warn "in /etc/init.d must be written manually."
fi

msg "Done: @@PKGNAME@@"
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pkgbuild", help="path to the PKGBUILD")
    ap.add_argument("-o", "--output", help="output script path")
    ap.add_argument("--map", help="extra Arch->Alpine package map file")
    args = ap.parse_args()

    text = Path(args.pkgbuild).read_text()
    funcs, top = split_functions(text)
    env = parse_vars(top)
    table = {**ARCH_TO_ALPINE, **load_extra_map(args.map)}

    pkgname = as_list(env.get("pkgname"))[0] if env.get("pkgname") else "package"
    env["pkgname"] = pkgname
    script = args.output or f"install-{pkgname}.sh"

    pkg_func = "package" if "package" in funcs else next(
        (f for f in funcs if f.startswith("package_")), None)
    phases = [f"run_phase {f}" for f in ("prepare", "build") if f in funcs]
    if pkg_func:
        phases.append(f"run_phase {pkg_func}")

    notes = [f"#   optdepends (not installed): {o}"
             for o in as_list(env.get("optdepends"))]
    if pkg_func and re.search(r"systemd|sysusers|tmpfiles", funcs[pkg_func]):
        notes.append("#   package() installs systemd units; Alpine uses OpenRC.")

    wanted = {"pkgver", "prepare", "build", pkg_func}
    body = "\n\n".join(funcs[f] for f in funcs if f in wanted)

    out = TEMPLATE
    for key, val in {
        "@@PKGNAME@@": pkgname,
        "@@SCRIPT@@": script,
        "@@NOTES@@": "\n".join(notes),
        "@@VARS@@": var_lines(env),
        "@@RUNTIME_DEPS@@": dep_lines(as_list(env.get("depends")), table),
        "@@BUILD_DEPS@@": dep_lines(
            as_list(env.get("makedepends")) + as_list(env.get("checkdepends")), table),
        "@@SOURCES@@": "\n".join(
            "  " + shlex.quote(classify_source(s)) for s in as_list(env.get("source"))),
        "@@FUNCTIONS@@": body,
        "@@PHASES@@": "\n".join(phases),
    }.items():
        out = out.replace(key, val)

    path = Path(script)
    path.write_text(out)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"wrote {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

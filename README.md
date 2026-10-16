# AuPine

pkgbuild2alpine.py reads a PKGBUILD without executing it and writes a standalone Alpine install script. Usage is ./pkgbuild2alpine.py PKGBUILD [-o out.sh] [--map extra.map].

**Dependencies:** each Arch dependency is mapped to Alpine candidates (for example python-yaml becomes py3-yaml). The generated script checks which candidates exist in your repos and installs them with apk, adding the -dev package for build dependencies.

**Missing packages and git sources:** dependencies not in the repos, and every git+ source, get a prompt showing the git clone command. The script waits for you to clone as your regular user, then carries on.

**Build and install:** the PKGBUILD's pkgver(), build() and package() run unmodified into a staging directory, which is then copied into /. It also translates sysusers and tmpfiles entries into adduser and mkdir/chown. Unresolved dependencies are built from your clone, with the build system auto-detected.

**Limits:** systemd units are only flagged with a warning, since Alpine uses OpenRC. += assignments aren't parsed.


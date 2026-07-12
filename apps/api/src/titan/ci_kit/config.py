"""``.titan-ci.toml`` — the only thing a generated workspace configures.

The kit is deliberately almost unconfigurable. The constitution FIXES the layer
names (§6.1 TITAN-WFL-022: domain, application, infrastructure, interface) and
the coverage thresholds (§6.5 TITAN-WFL-030: >=80% line / >=70% branch), so
those are not knobs — a workspace that could rename its layers or lower its
thresholds could opt out of the constitution, which is the one thing this kit
exists to prevent.

What a workspace legitimately varies is only *where its code lives*: the root
package, the source dir, the migrations dir, and the branch CI diffs against.
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_FILENAME = ".titan-ci.toml"

# §6.1 TITAN-WFL-022, outermost first. import-linter's `layers` contract reads
# top-to-bottom as high-to-low, and a lower layer importing a higher one is the
# violation — so this order IS the "dependencies point inward only" rule.
LAYERS: tuple[str, ...] = ("interface", "infrastructure", "application", "domain")

# §6.1 TITAN-WFL-025: the one layer allowed to import an ORM.
INFRASTRUCTURE_LAYER = "infrastructure"

# §6.5 TITAN-WFL-030. Constitutional floors, not defaults to be negotiated down.
MIN_LINE_COVERAGE = 80.0
MIN_BRANCH_COVERAGE = 70.0


class ConfigError(Exception):
    """The workspace's .titan-ci.toml is missing or malformed."""


@dataclass(frozen=True)
class KitConfig:
    """Where a generated workspace keeps its code. Everything else is fixed."""

    package: str
    """The Python root package holding the four layers, e.g. ``billing``."""

    src: str = "src"
    """Source root, relative to the workspace, e.g. ``src`` for src-layout."""

    migrations: str = "migrations"
    """Directory the SQL migration linter scans."""

    base_ref: str = "origin/main"
    """The ref CI diffs against for the changed-code coverage gate."""

    ts_src: str = field(default="")
    """Optional TypeScript source root; empty disables the ESLint boundary gate."""

    @property
    def package_root(self) -> Path:
        """Workspace-relative path of the root package, e.g. ``src/billing``."""
        return Path(self.src) / self.package

    def layer_path(self, layer: str) -> Path:
        return self.package_root / layer


def parse_config(text: str) -> KitConfig:
    """Parse .titan-ci.toml. Pure — the file-reading half lives in load_config."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{CONFIG_FILENAME} is not valid TOML: {exc}") from exc

    section = data.get("titan-ci")
    if not isinstance(section, dict):
        raise ConfigError(f"{CONFIG_FILENAME} must contain a [titan-ci] table")

    package = section.get("package")
    if not isinstance(package, str) or not package:
        raise ConfigError("[titan-ci] package is required (the Python root package name)")

    def _str(key: str, default: str) -> str:
        value = section.get(key, default)
        if not isinstance(value, str):
            raise ConfigError(f"[titan-ci] {key} must be a string")
        return value

    return KitConfig(
        package=package,
        src=_str("src", "src"),
        migrations=_str("migrations", "migrations"),
        base_ref=_str("base_ref", "origin/main"),
        ts_src=_str("ts_src", ""),
    )


def load_config(workspace: Path) -> KitConfig:
    """Read .titan-ci.toml from a workspace root."""
    path = workspace / CONFIG_FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(
            f"no {CONFIG_FILENAME} at {workspace} — run `titan-ci-init` first"
        ) from exc
    return parse_config(text)


__all__ = [
    "CONFIG_FILENAME",
    "INFRASTRUCTURE_LAYER",
    "LAYERS",
    "MIN_BRANCH_COVERAGE",
    "MIN_LINE_COVERAGE",
    "ConfigError",
    "KitConfig",
    "load_config",
    "parse_config",
]

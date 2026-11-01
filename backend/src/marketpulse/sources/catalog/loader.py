"""Load and validate the catalog. All problems are collected and reported together."""

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from pydantic import ValidationError

from marketpulse.sources.catalog.schema import (
    SERIES_ID_RULES,
    SeriesFile,
    SeriesSpec,
    SourcesFile,
    SourceSpec,
)

DEFAULT_CATALOG_DIR = Path(__file__).resolve().parent / "data"


class CatalogError(Exception):
    """The catalog is invalid. `problems` lists every issue found."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__(
            f"Catalog has {len(problems)} problem(s):\n" + "\n".join(f"  - {p}" for p in problems)
        )


@dataclass(frozen=True)
class Catalog:
    sources: dict[str, SourceSpec]
    series: tuple[tuple[str, SeriesSpec], ...]  # (source code, spec)

    def series_for(self, source: str) -> list[SeriesSpec]:
        return [spec for code, spec in self.series if code == source]


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys (PyYAML silently keeps the last one)."""


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node, deep=True)


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _read_yaml(path: Path, problems: list[str]) -> Any:
    try:
        return yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)  # noqa: S506 (safe loader subclass)
    except yaml.YAMLError as exc:
        problems.append(f"{path.name}: invalid YAML: {exc}")
        return None


def _format_validation_error(path: Path, exc: ValidationError) -> list[str]:
    return [
        f"{path.name}: {'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
    ]


def load_catalog(catalog_dir: Path = DEFAULT_CATALOG_DIR) -> Catalog:
    """Load `sources.yaml` and `series/*.yaml`, validating everything. Raises CatalogError."""
    problems: list[str] = []

    sources_path = catalog_dir / "sources.yaml"
    sources_file: SourcesFile | None = None
    if not sources_path.is_file():
        problems.append(f"missing {sources_path.name}")
    else:
        raw = _read_yaml(sources_path, problems)
        if raw is not None:
            try:
                sources_file = SourcesFile.model_validate(raw)
            except ValidationError as exc:
                problems.extend(_format_validation_error(sources_path, exc))

    sources: dict[str, SourceSpec] = {}
    if sources_file:
        for code, count in Counter(s.code for s in sources_file.sources).items():
            if count > 1:
                problems.append(f"sources.yaml: duplicate source code {code!r}")
        sources = {s.code: s for s in sources_file.sources}

    series: list[tuple[str, SeriesSpec, str]] = []  # (source, spec, file name)
    series_files = sorted((catalog_dir / "series").glob("*.yaml"))
    if not series_files:
        problems.append("no series files found in series/")
    for path in series_files:
        raw = _read_yaml(path, problems)
        if raw is None:
            continue
        try:
            series_file = SeriesFile.model_validate(raw)
        except ValidationError as exc:
            problems.extend(_format_validation_error(path, exc))
            continue
        if path.stem != series_file.source:
            problems.append(
                f"{path.name}: file name must match its source ({series_file.source!r})"
            )
        if sources and series_file.source not in sources:
            problems.append(f"{path.name}: unknown source {series_file.source!r}")
        series.extend((series_file.source, spec, path.name) for spec in series_file.series)

    problems.extend(_cross_checks(sources, series))
    if problems:
        raise CatalogError(problems)
    return Catalog(sources=sources, series=tuple((src, spec) for src, spec, _ in series))


def _cross_checks(
    sources: dict[str, SourceSpec], series: list[tuple[str, SeriesSpec, str]]
) -> list[str]:
    problems: list[str] = []

    codes = Counter(spec.code for _, spec, _ in series)
    problems += [f"duplicate series code {code!r}" for code, n in codes.items() if n > 1]

    ids = Counter((src, spec.source_series_id) for src, spec, _ in series)
    problems += [
        f"duplicate series ID {sid!r} for source {src!r}" for (src, sid), n in ids.items() if n > 1
    ]

    for src, spec, file_name in series:
        where = f"{file_name}: {spec.code}"
        rule = SERIES_ID_RULES.get(src)
        if rule:
            problems += [f"{where}: {p}" for p in rule(spec)]
        source = sources.get(src)
        if source:
            host = urlsplit(spec.reference_url).hostname or ""
            if not any(host == h or host.endswith("." + h) for h in source.reference_hosts):
                problems.append(
                    f"{where}: reference_url host {host!r} is not an official "
                    f"{src} host {list(source.reference_hosts)}"
                )
    return problems

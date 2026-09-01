"""Serve parsed API specs to the agent.

Retrieval is two-stage and deterministic:

1. :meth:`SpecStore.catalog_prompt` renders the whole ~340-row catalog compactly
   enough to fit one prompt, so the model picks API IDs by reading the menu.
2. :meth:`SpecStore.get` loads those exact specs by key -- no similarity search,
   so there is no recall risk on the field tables the caller actually needs.

:meth:`SpecStore.search` is a keyword fallback for fuzzy asks; it never replaces
step 1 as the primary path.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path

from trading.rag.spec_parser import ApiSpec, CatalogEntry, Field, Market, parse_workbook

DEFAULT_INDEX = Path("data/specs/kiwoom.json")

_ALIASES = {"공통": "오류코드"}


@dataclass(slots=True)
class SpecStore:
    catalog: list[CatalogEntry]
    specs: dict[str, ApiSpec]

    # -- construction ---------------------------------------------------------

    @classmethod
    def from_workbook(cls, path: str | Path) -> SpecStore:
        catalog, specs = parse_workbook(path)
        return cls(catalog=catalog, specs=specs)

    @classmethod
    def load(cls, path: str | Path = DEFAULT_INDEX) -> SpecStore:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        catalog = [CatalogEntry(**e) for e in raw["catalog"]]
        specs = {}
        for api_id, s in raw["specs"].items():
            s = dict(s)
            s["request"] = [Field(**f) for f in s.get("request", [])]
            s["response"] = [Field(**f) for f in s.get("response", [])]
            specs[api_id] = ApiSpec(**s)
        return cls(catalog=catalog, specs=specs)

    def save(self, path: str | Path = DEFAULT_INDEX) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "catalog": [dataclasses.asdict(e) for e in self.catalog],
            "specs": {k: dataclasses.asdict(v) for k, v in self.specs.items()},
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        return path

    # -- retrieval ------------------------------------------------------------

    def get(self, api_id: str) -> ApiSpec:
        # The catalog lists the error-code reference under `공통`, but its sheet
        # carries no API ID and is keyed by sheet name.
        key = _ALIASES.get(api_id, api_id)
        try:
            return self.specs[key]
        except KeyError:
            raise KeyError(
                f"unknown API id {api_id!r}; call catalog_prompt() to list valid ids"
            ) from None

    def entries(self, market: Market | None = None) -> list[CatalogEntry]:
        if market is None:
            return list(self.catalog)
        return [e for e in self.catalog if e.market is market]

    def search(
        self, query: str, limit: int = 10, market: Market | None = None
    ) -> list[CatalogEntry]:
        """Substring match over catalog text. Fallback path -- see module docstring."""
        q = query.strip().lower()
        if not q:
            return []
        hits = [
            e
            for e in self.entries(market)
            if q in e.name.lower()
            or q in e.api_id.lower()
            or q in e.category.lower()
            or q in e.subcategory.lower()
        ]
        return hits[:limit]

    def catalog_prompt(self, market: Market | None = None) -> str:
        """Render the catalog as one line per API for the routing prompt.

        Always scope by market when routing: KR and US are separate surfaces, and
        an unscoped catalog invites the model to answer a KR question with a US id.
        """
        lines = [
            f"{e.api_id}\t{e.name}\t{e.category}>{e.subcategory}" for e in self.entries(market)
        ]
        return "\n".join(lines)

    def spec_prompt(self, api_id: str) -> str:
        """Render one spec as the model-facing reference for building a call."""
        s = self.get(api_id)
        out = [
            f"# {s.api_id} — {s.name}",
            f"{s.method} {s.url}    ({s.menu_path})",
        ]
        if s.overview:
            out.append(s.overview)

        def table(title: str, fields: list[Field]) -> None:
            body = [f for f in fields if f.section == "Body"]
            if not body:
                return
            out.append(f"\n## {title}")
            for f in body:
                mark = "  - " if f.parent else "  "
                req = "required" if f.required else "optional"
                desc = f" — {f.description}" if f.description else ""
                out.append(f"{mark}{f.element} ({f.korean_name}, {req}){desc}")

        table("Request body", s.request)
        table("Response body", s.response)
        if s.request_example:
            out.append(f"\n## Request example\n{s.request_example}")
        return "\n".join(out)

    # -- validation used by the broker client --------------------------------

    def validate_body(self, api_id: str, body: dict) -> None:
        """Raise ValueError if `body` violates the parsed spec."""
        spec = self.get(api_id)
        known, required = spec.known_body(), set(spec.required_body())
        missing = required - body.keys()
        if missing:
            raise ValueError(f"{api_id}: missing required field(s) {sorted(missing)}")
        unknown = body.keys() - known
        if unknown:
            raise ValueError(
                f"{api_id}: unknown field(s) {sorted(unknown)}; valid: {sorted(known)}"
            )
        for f in spec.body_fields():
            if f.parent is None and f.length.isdigit() and f.element in body:
                val = str(body[f.element])
                if len(val) > int(f.length):
                    raise ValueError(
                        f"{api_id}: field {f.element!r} is {len(val)} chars, spec max is {f.length}"
                    )

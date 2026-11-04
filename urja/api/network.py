"""Network hierarchy routes.

The portal has no hierarchy endpoint: each meter carries a flat path
(zone → circle → division → subdivision → substation → feeder → DT). Exposing a
real tree means reconstructing it here, and being explicit about the blanks the
source data contains — some meters have no feeder code at all, and a few DTs are
spelled differently in different endpoints.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Path, Query

from urja.db import store
from urja.models import HierarchyResponse, HierarchyTreeNode, MeterListResponse, PageInfo, ResponseMeta
from urja.portal.parse import to_iso

router = APIRouter(tags=["network"])

LEVELS = ("zone", "circle", "division", "subdivision", "substation", "feeder", "dt")

Level = Literal["zone", "circle", "division", "subdivision", "substation", "feeder", "dt"]


def _build_tree(paths: list[dict[str, Any]], depth: int) -> list[HierarchyTreeNode]:
    roots: dict[str, dict[str, Any]] = {}

    for row in paths:
        count = int(row.get("meters") or 0)
        level_map = roots
        for level in LEVELS[: depth + 1]:
            code = row.get(f"{level}_code")
            name = row.get(f"{level}_name")
            key = code or f"__unassigned_{level}"
            node = level_map.setdefault(
                key, {"code": code, "name": name, "meters": 0, "children": {}}
            )
            node["meters"] += count
            level_map = node["children"]

    def to_model(node: dict[str, Any]) -> HierarchyTreeNode:
        return HierarchyTreeNode(
            code=node["code"],
            name=node["name"],
            meters=node["meters"],
            children=[
                to_model(child)
                for _, child in sorted(node["children"].items(), key=lambda item: item[0])
            ],
        )

    return [to_model(root) for _, root in sorted(roots.items())]


@router.get(
    "/network/hierarchy",
    response_model=HierarchyResponse,
    summary="Network hierarchy tree",
    description=(
        "The zone → … → DT tree reconstructed from per-meter paths, with meter "
        "counts at every level.\n\n"
        "`to` trims the tree: `to=feeder` returns zones through feeders only. "
        "Nodes whose source code was blank are emitted with `code: null` rather "
        "than dropped, so the meter counts always add up to the population."
    ),
)
def hierarchy(
    to: Level = Query("dt", description="Deepest level to include."),
) -> HierarchyResponse:
    paths = store.hierarchy_counts()["paths"]
    return HierarchyResponse(
        generatedAt=to_iso(),
        dataAgeSeconds=store.data_age_seconds(),
        depth=to,
        roots=_build_tree(paths, LEVELS.index(to)),
    )


@router.get(
    "/network/feeders/{feeder_code}/meters",
    response_model=MeterListResponse,
    summary="Meters on a feeder",
    description="Convenience view over `/meters?feeder=…`, including pagination metadata.",
)
def feeder_meters(
    feeder_code: str = Path(..., description="Feeder code, e.g. `F-012`."),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
) -> MeterListResponse:
    from urja.api.meters import _summary  # local import avoids a circular import

    rows, next_cursor, total = store.list_meters(feeder=feeder_code, limit=limit, cursor=cursor)
    return MeterListResponse(
        data=[_summary(row) for row in rows],
        pageInfo=PageInfo(nextCursor=next_cursor, limit=limit),
        meta=ResponseMeta(generatedAt=to_iso(), dataAgeSeconds=store.data_age_seconds(), total=total),
    )

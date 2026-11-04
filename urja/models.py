"""Public API schemas.

These models *are* the contract: FastAPI derives ``openapi.json`` and the
``/docs`` page from them, so the spec can never drift from the responses.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Phase = Literal["single", "three", "unknown"]
Status = str


class HierarchyNode(BaseModel):
    code: str | None = Field(None, description="Portal code, e.g. `F-001`. Null when the source row was blank.")
    name: str | None = None


class MeterHierarchy(BaseModel):
    """Where the meter sits in the network, root to leaf.

    Reconstructed from the paths the portal returns; the portal itself has no
    hierarchy endpoint. Levels whose codes are blank in the source are null
    rather than silently dropped.
    """

    zone: HierarchyNode | None = None
    circle: HierarchyNode | None = None
    division: HierarchyNode | None = None
    subdivision: HierarchyNode | None = None
    substation: HierarchyNode | None = None
    feeder: HierarchyNode | None = None
    dt: HierarchyNode | None = None


class Location(BaseModel):
    latitude: float | None = None
    longitude: float | None = None


class ReadingSummary(BaseModel):
    first_reading_at: str | None = Field(None, alias="firstReadingAt")
    last_reading_at: str | None = Field(None, alias="lastReadingAt")
    count: int = 0
    granularity_seconds: int | None = Field(
        None,
        alias="granularitySeconds",
        description="Native sampling interval. Varies **per meter** in the source data (1800 or 86400).",
    )
    granularity: Literal["30m", "1d", "other", "none"] = "none"

    model_config = ConfigDict(populate_by_name=True)


class MeterSummary(BaseModel):
    id: str = Field(..., alias="meterId")
    serial: str | None = None
    make: str | None = None
    phase: Phase = "unknown"
    status: Status = Field("unknown", description="`installed` | `faulty` | `decommissioned`")
    installation: str | None = Field(None, description="`whole_current` | `ct_operated`")
    dt_code: str | None = Field(None, alias="dtCode")
    feeder_code: str | None = Field(None, alias="feederCode")
    reading_summary: ReadingSummary | None = Field(None, alias="readingSummary")

    model_config = ConfigDict(populate_by_name=True)


class MeterDetail(MeterSummary):
    hierarchy: MeterHierarchy
    location: Location | None = None
    data_quality: list[str] = Field(
        default_factory=list,
        alias="dataQuality",
        description="Normalisation notes for this row, e.g. `FEEDER_REPAIRED_FROM_DT`.",
    )
    synced_at: str | None = Field(None, alias="syncedAt")


class PageInfo(BaseModel):
    next_cursor: str | None = Field(None, alias="nextCursor", description="Pass as `cursor` to fetch the next page. Null on the last page.")
    limit: int


class ResponseMeta(BaseModel):
    generated_at: str = Field(..., alias="generatedAt")
    data_age_seconds: int | None = Field(
        None,
        alias="dataAgeSeconds",
        description="Seconds since this index last completed a sync. Null before the first sync.",
    )
    total: int | None = Field(None, description="Total matching records, ignoring pagination.")


class MeterListResponse(BaseModel):
    data: list[MeterSummary]
    page_info: PageInfo = Field(..., alias="pageInfo")
    meta: ResponseMeta


class ConsumptionPoint(BaseModel):
    start: str = Field(..., description="Start of the interval (ISO-8601, +05:30).")
    end: str = Field(..., description="End of the interval (ISO-8601, +05:30).")
    kwh: float | None = Field(None, description="Energy **consumed** in the interval, derived from the cumulative register.")
    kvah: float | None = None
    derived: bool = Field(True, description="True when computed as the difference between two register readings.")
    anomaly: str | None = Field(None, description="e.g. `register_reset` when the meter's register went backwards.")


class ConsumptionMeta(BaseModel):
    source_granularity: str | None = Field(None, alias="sourceGranularity", description="Native interval of the source readings for this meter.")
    source_timezone: str = Field("Asia/Kolkata", alias="sourceTimezone")
    unit: str = "kWh"
    data_ends_at: str | None = Field(None, alias="dataEndsAt", description="Newest reading available for this meter.")
    suppressed_points: int = Field(0, alias="suppressedPoints", description="Intervals dropped because a register value was missing.")

    model_config = ConfigDict(populate_by_name=True)


class ConsumptionResponse(BaseModel):
    meter_id: str = Field(..., alias="meterId")
    interval: Literal["auto", "30m", "1d"]
    data: list[ConsumptionPoint]
    meta: ConsumptionMeta


class HierarchyTreeNode(BaseModel):
    code: str | None
    name: str | None
    meters: int = Field(..., description="Meters at or below this node.")
    children: list["HierarchyTreeNode"] = Field(default_factory=list)


class HierarchyResponse(BaseModel):
    generated_at: str = Field(..., alias="generatedAt")
    data_age_seconds: int | None = Field(None, alias="dataAgeSeconds")
    depth: str = Field(..., description="Deepest level included, e.g. `feeder`.")
    roots: list[HierarchyTreeNode]


class SyncCounts(BaseModel):
    meters: int
    readings: int
    first_reading_at: str | None = Field(None, alias="firstReadingAt")
    last_reading_at: str | None = Field(None, alias="lastReadingAt")

    model_config = ConfigDict(populate_by_name=True)


class PortalState(BaseModel):
    configured: bool = Field(..., description="Whether portal credentials are present in the environment.")


class SyncStatus(BaseModel):
    last_success_at: str | None = Field(None, alias="lastSuccessAt")
    data_age_seconds: int | None = Field(None, alias="dataAgeSeconds")
    running: bool
    last_run: dict | None = Field(None, alias="lastRun", description="Last `sync_runs` row, plus in-flight progress if a sync is running.")
    counts: SyncCounts
    portal: PortalState

    model_config = ConfigDict(populate_by_name=True)


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    indexed_meters: int = Field(..., alias="indexedMeters")
    indexed_points: int = Field(..., alias="indexedPoints")

    model_config = ConfigDict(populate_by_name=True)


HierarchyTreeNode.model_rebuild()

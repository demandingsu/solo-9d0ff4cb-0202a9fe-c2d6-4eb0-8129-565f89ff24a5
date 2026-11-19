"""API 请求/响应模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SegmentIn(BaseModel):
    lo: int = Field(..., ge=0, description="区间起点，包含，整数微米")
    hi: int = Field(..., description="区间终点，不包含，整数微米，必须大于 lo")
    count: int = Field(..., ge=0, description="该厚度区间内的片数")

    @model_validator(mode="after")
    def _validate_range(self) -> "SegmentIn":
        if self.hi <= self.lo:
            raise ValueError("hi 必须大于 lo（区间含起点、不含终点）")
        return self


class CountReportIn(BaseModel):
    segments: list[SegmentIn] = Field(
        ...,
        min_length=1,
        description="本次补报的全部厚度区间计数；同一报告内或与已有报告可以重叠。"
        "任一区间与已有计数不相容时，整份报告拒收。",
    )


class InspectionCreate(BaseModel):
    total_pieces: int = Field(..., ge=0, description="纸样总片数")
    note: str | None = Field(None, max_length=500, description="备注，可选")


class InspectionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_pieces: int | None = Field(None, ge=0, description="新的总片数，可选")
    note: str | None = Field(None, max_length=500, description="新的备注；传 null 可清空")

    @model_validator(mode="after")
    def _validate_payload(self) -> "InspectionUpdate":
        if not self.model_fields_set:
            raise ValueError("至少提供 total_pieces 或 note 中的一项")
        return self


class SegmentOut(BaseModel):
    id: int
    lo: int
    hi: int
    count: int


class CountReportOut(BaseModel):
    id: int
    inspection_id: int
    created_at: str
    segments: list[SegmentOut]


class InspectionSummary(BaseModel):
    id: int
    total_pieces: int
    note: str | None
    created_at: str
    report_count: int = Field(..., description="已接收的补报批次数")
    segment_count: int = Field(..., description="已接收的厚度区间条数")


class InspectionDetail(InspectionSummary):
    reports: list[CountReportOut]


class BoundsOut(BaseModel):
    inspection_id: int
    total_pieces: int
    qualified_lo: int
    qualified_hi: int
    min_count: int = Field(..., description="同时满足全部计数时，合格片数可达到的最小值")
    max_count: int = Field(..., description="同时满足全部计数时，合格片数可达到的最大值")


# ---------------------------------------------------------------------------
# 复卷纸追溯
# ---------------------------------------------------------------------------


class RawRollCreate(BaseModel):
    kind: Literal["raw"] = Field("raw", description="原卷登记固定为 raw")
    length_mm: int = Field(..., ge=1, description="原卷登记长度，毫米，从卷头起算")
    inspection_id: int = Field(
        ..., ge=1, description="关联的已有质检登记号（/inspections 的 id）"
    )
    note: str | None = Field(None, max_length=500, description="备注，可选")


class SpliceIn(BaseModel):
    source_roll_id: int = Field(
        ..., ge=1, description="来源卷 id；可以是原卷，也可以是另一成卷"
    )
    source_lo: int = Field(..., ge=0, description="来源卷截取区间起点，毫米，包含")
    source_hi: int = Field(..., ge=1, description="来源卷截取区间终点，毫米，不包含")
    reversed: bool = Field(
        ..., description="是否掉头：true 表示该段相对来源卷反向接入成卷"
    )

    @model_validator(mode="after")
    def _validate_range(self) -> "SpliceIn":
        if self.source_hi <= self.source_lo:
            raise ValueError("source_hi 必须大于 source_lo（区间含起点、不含终点）")
        return self


class FinishedRollCreate(BaseModel):
    kind: Literal["finished"] = Field("finished", description="成卷登记固定为 finished")
    note: str | None = Field(None, max_length=500, description="备注，可选")
    splices: list[SpliceIn] = Field(
        ...,
        min_length=1,
        description=(
            "按接入顺序给出的接段；成卷位置从卷头起按毫米计，"
            "每段依次首尾相接，区间含起点不含终点。"
        ),
    )


class RawRollUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    length_mm: int | None = Field(None, ge=1, description="更正后的原卷长度，毫米")
    note: str | None = Field(None, max_length=500, description="新的备注；传 null 可清空")

    @model_validator(mode="after")
    def _validate_payload(self) -> "RawRollUpdate":
        if not self.model_fields_set:
            raise ValueError("至少提供 length_mm 或 note 中的一项")
        return self


class FinishedRollUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(None, max_length=500, description="新的备注；传 null 可清空")

    @model_validator(mode="after")
    def _validate_payload(self) -> "FinishedRollUpdate":
        if not self.model_fields_set:
            raise ValueError("成卷仅支持更正备注，至少提供 note 一项")
        return self


class RollUpdate(BaseModel):
    """卷更正的统一入口：按卷类型在端点内分流。

    原卷接受 length_mm/note；成卷仅接受 note，传 length_mm 会被拒绝。
    """

    model_config = ConfigDict(extra="forbid")

    length_mm: int | None = Field(None, ge=1, description="更正后的原卷长度，毫米（仅原卷）")
    note: str | None = Field(None, max_length=500, description="新的备注；传 null 可清空")

    @model_validator(mode="after")
    def _validate_payload(self) -> "RollUpdate":
        if not self.model_fields_set:
            raise ValueError("至少提供 length_mm 或 note 中的一项")
        return self


class SplicePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_roll_id: int | None = Field(None, ge=1, description="更正后的来源卷 id")
    source_lo: int | None = Field(None, ge=0, description="更正后的截取区间起点，包含")
    source_hi: int | None = Field(None, ge=1, description="更正后的截取区间终点，不包含")
    reversed: bool | None = Field(None, description="更正后的掉头标记")

    @model_validator(mode="after")
    def _validate_payload(self) -> "SplicePatch":
        if not self.model_fields_set:
            raise ValueError(
                "至少提供 source_roll_id/source_lo/source_hi/reversed 中的一项"
            )
        return self


class SpliceOut(BaseModel):
    id: int
    roll_id: int = Field(..., description="所属成卷 id")
    seq: int = Field(..., description="接入顺序，从 1 开始")
    start: int = Field(..., description="该段在成卷上的起点（含），毫米")
    end: int = Field(..., description="该段在成卷上的终点（不含），毫米")
    source_roll_id: int
    source_lo: int
    source_hi: int
    reversed: bool


class RollSummary(BaseModel):
    id: int
    kind: Literal["raw", "finished"]
    length_mm: int = Field(
        ..., description="卷长（毫米）：原卷为登记/更正长度，成卷为各接段长度之和"
    )
    inspection_id: int | None = Field(
        None, description="原卷关联的质检登记号；成卷为 null"
    )
    note: str | None
    created_at: str
    splice_count: int = Field(..., description="成卷已登记的接段数；原卷恒为 0")


class RollDetail(RollSummary):
    splices: list[SpliceOut]


class TraceItem(BaseModel):
    raw_roll_id: int = Field(..., description="追溯到的原卷 id（原卷号）")
    inspection_id: int = Field(..., description="该原卷关联的质检登记号")
    raw_lo: int = Field(..., description="原卷上的区间起点，毫米，包含")
    raw_hi: int = Field(..., description="原卷上的区间终点，毫米，不包含")
    direction: Literal["正向", "反向"] = Field(
        ..., description="相对原卷卷头的方向（掉头次数为奇数即反向）"
    )
    reversed: bool = Field(..., description="方向是否反向（与 direction 等价的布尔值）")


class TraceOut(BaseModel):
    roll_id: int = Field(..., description="被查询的成卷 id")
    query_lo: int
    query_hi: int
    items: list[TraceItem] = Field(
        ..., description="按成卷顺序排列的追溯结果；接缝处自动分段"
    )

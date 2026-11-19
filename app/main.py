"""FastAPI 应用：纸样厚度计数核对与复卷纸追溯。

纸样厚度计数
------------
* ``POST /inspections``                    登记一批纸样的总片数
* ``GET  /inspections``                    列出全部登记
* ``GET  /inspections/{id}``               查看登记及已接收的分次补报
* ``PATCH /inspections/{id}``              更正总片数/备注（与已有计数冲突则拒收）
* ``POST /inspections/{id}/reports``       分次提交厚度区间计数（整份原子接收或拒收）
* ``GET  /inspections/{id}/reports``       查看补报历史
* ``GET  /inspections/{id}/bounds``        给定合格区间，查询合格片数最少/最多

复卷纸追溯
----------
* ``POST /rolls/raw``                      登记原卷长度并关联已有质检登记号
* ``POST /rolls/finished``                 登记成卷及其按接入顺序的接段
* ``GET  /rolls``                          列出全部原卷与成卷
* ``GET  /rolls/{id}``                     查看卷信息与接段（含成卷上的起止位置）
* ``PATCH /rolls/{id}``                    更正原卷长度/备注或成卷备注（冲突整次拒收）
* ``POST /rolls/{id}/splices``             向成卷追加接段（来源可为原卷或另一成卷）
* ``PATCH /splices/{id}``                  更正接段（循环或下游越界则整次拒收）
* ``GET  /rolls/{id}/trace``               追溯成卷一段的原卷来源与质检登记号
* ``GET  /health``                         健康检查

所有区间均为整数，含起点不含终点，即 ``[lo, hi)``；
厚度计数单位为微米，卷位置单位为毫米。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, status
from sqlite3 import Connection

from . import db, traceability
from .schemas import (
    BoundsOut,
    CountReportIn,
    CountReportOut,
    FinishedRollCreate,
    InspectionCreate,
    InspectionDetail,
    InspectionSummary,
    InspectionUpdate,
    RawRollCreate,
    RollDetail,
    RollSummary,
    RollUpdate,
    SpliceIn,
    SpliceOut,
    SplicePatch,
    TraceOut,
)
from .solver import InconsistentCounts, check_consistent, compute_bounds
from .traceability import TraceabilityError

DEFAULT_DB_PATH = os.environ.get("DB_PATH", "./data/inspection.db")


def create_app(db_path: str | None = None) -> FastAPI:
    path = db_path or DEFAULT_DB_PATH

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        db.init(path)
        yield

    api = FastAPI(
        title="纸样厚度计数核对与复卷纸追溯",
        description=(
            "登记总片数，分次补报互可重叠的厚度区间计数；"
            "按全部计数的交集计算合格片数的可达到上下界，不逐片展开。"
            "另支持复卷纸追溯：登记原卷长度与质检登记号、成卷接段（来源可为"
            "另一成卷，可掉头），按成卷区间追溯原卷号、区间、方向与质检登记号。"
        ),
        version="2.0.0",
        lifespan=lifespan,
    )

    def get_conn():
        connection = db.connect(path)
        try:
            yield connection
        finally:
            connection.close()

    def _require_inspection(connection: Connection, inspection_id: int):
        row = db.get_inspection(connection, inspection_id)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"登记 {inspection_id} 不存在",
            )
        return row

    def _summary(row) -> InspectionSummary:
        return InspectionSummary(
            id=row["id"],
            total_pieces=row["total_pieces"],
            note=row["note"],
            created_at=row["created_at"],
            report_count=row["report_count"],
            segment_count=row["segment_count"],
        )

    @api.get("/health", tags=["运维"], summary="健康检查")
    def health() -> dict:
        return {"status": "ok"}

    @api.post(
        "/inspections",
        response_model=InspectionSummary,
        status_code=status.HTTP_201_CREATED,
        tags=["登记"],
        summary="登记一批纸样的总片数",
    )
    def create_inspection(
        payload: InspectionCreate, connection: Connection = Depends(get_conn)
    ) -> InspectionSummary:
        inspection_id = db.create_inspection(
            connection, payload.total_pieces, payload.note
        )
        row = _require_inspection(connection, inspection_id)
        return _summary(row)

    @api.get(
        "/inspections",
        response_model=list[InspectionSummary],
        tags=["登记"],
        summary="列出全部登记",
    )
    def list_inspections(
        connection: Connection = Depends(get_conn),
    ) -> list[InspectionSummary]:
        return [_summary(row) for row in db.list_inspections(connection)]

    @api.get(
        "/inspections/{inspection_id}",
        response_model=InspectionDetail,
        tags=["登记"],
        summary="查看登记及已接收的补报",
    )
    def get_inspection(
        inspection_id: int, connection: Connection = Depends(get_conn)
    ) -> InspectionDetail:
        row = _require_inspection(connection, inspection_id)
        summary = _summary(row)
        reports = [CountReportOut(**r) for r in db.get_reports(connection, inspection_id)]
        return InspectionDetail(**summary.model_dump(), reports=reports)

    @api.patch(
        "/inspections/{inspection_id}",
        response_model=InspectionSummary,
        tags=["登记"],
        summary="更正总片数或备注",
        description=(
            "若新的总片数与已接收的计数不相容，整次更正被拒收，原登记保持不变。"
        ),
    )
    def update_inspection(
        inspection_id: int,
        payload: InspectionUpdate,
        connection: Connection = Depends(get_conn),
    ) -> InspectionSummary:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = _require_inspection(connection, inspection_id)
            new_total = (
                payload.total_pieces
                if "total_pieces" in payload.model_fields_set
                else int(row["total_pieces"])
            )
            new_note = payload.note if "note" in payload.model_fields_set else row["note"]
            if "total_pieces" in payload.model_fields_set:
                constraints = db.load_constraints(connection, inspection_id)
                try:
                    check_consistent(new_total, constraints)
                except InconsistentCounts:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            f"新总片数 {new_total} 与已接收的计数不相容，"
                            "本次更正已拒收，原登记保持不变"
                        ),
                    )
            db.update_inspection(connection, inspection_id, new_total, new_note)
            connection.execute("COMMIT")
        except HTTPException:
            connection.execute("ROLLBACK")
            raise
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        row = _require_inspection(connection, inspection_id)
        return _summary(row)

    @api.post(
        "/inspections/{inspection_id}/reports",
        response_model=CountReportOut,
        status_code=status.HTTP_201_CREATED,
        tags=["补报"],
        summary="提交一批厚度区间计数",
        description=(
            "区间可以重叠。整份报告要么全部接收，要么在与已有计数"
            "（含本报告内其他区间）不相容时整份拒收；拒收不改变已有结果。"
        ),
    )
    def submit_report(
        inspection_id: int,
        payload: CountReportIn,
        connection: Connection = Depends(get_conn),
    ) -> CountReportOut:
        segments = [(s.lo, s.hi, s.count) for s in payload.segments]

        connection.execute("BEGIN IMMEDIATE")
        try:
            row = _require_inspection(connection, inspection_id)
            existing = db.load_constraints(connection, inspection_id)
            try:
                check_consistent(int(row["total_pieces"]), existing + segments)
            except InconsistentCounts as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "补报与已有计数（或本报告内部）不相容，整份拒收；"
                        "已有结果保持不变"
                    ),
                ) from exc
            report_id = db.insert_report(connection, inspection_id, segments)
            connection.execute("COMMIT")
        except HTTPException:
            connection.execute("ROLLBACK")
            raise
        except BaseException:
            connection.execute("ROLLBACK")
            raise

        reports = db.get_reports(connection, inspection_id)
        for report in reports:
            if report["id"] == report_id:
                return CountReportOut(**report)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="补报写入后未能读回",
        )

    @api.get(
        "/inspections/{inspection_id}/reports",
        response_model=list[CountReportOut],
        tags=["补报"],
        summary="查看补报历史",
    )
    def list_reports(
        inspection_id: int, connection: Connection = Depends(get_conn)
    ) -> list[CountReportOut]:
        _require_inspection(connection, inspection_id)
        return [CountReportOut(**r) for r in db.get_reports(connection, inspection_id)]

    @api.get(
        "/inspections/{inspection_id}/bounds",
        response_model=BoundsOut,
        tags=["查询"],
        summary="查询合格片数的最少/最多可达到值",
        description=(
            "合格区间为 [qualified_lo, qualified_hi)，整数微米，含起点不含终点。"
            "返回值是同时满足全部已接收计数时，合格片数可同时达到的最小值和最大值；"
            "不逐片展开，按端点划分的整数规划精确求解。"
        ),
    )
    def get_bounds(
        inspection_id: int,
        qualified_lo: int = Query(..., ge=0, description="合格区间起点，包含"),
        qualified_hi: int = Query(..., description="合格区间终点，不包含，须大于起点"),
        connection: Connection = Depends(get_conn),
    ) -> BoundsOut:
        if qualified_hi <= qualified_lo:
            raise HTTPException(
                status_code=422,
                detail="qualified_hi 必须大于 qualified_lo（区间含起点、不含终点）",
            )
        row = _require_inspection(connection, inspection_id)
        constraints = db.load_constraints(connection, inspection_id)
        try:
            min_count, max_count = compute_bounds(
                int(row["total_pieces"]), constraints, qualified_lo, qualified_hi
            )
        except InconsistentCounts as exc:
            # 正常写入路径会拒绝不相容数据；此处防御数据库被外部改写。
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="已存计数互相矛盾，无法计算；请检查数据",
            ) from exc
        return BoundsOut(
            inspection_id=inspection_id,
            total_pieces=int(row["total_pieces"]),
            qualified_lo=qualified_lo,
            qualified_hi=qualified_hi,
            min_count=min_count,
            max_count=max_count,
        )

    # ------------------------------------------------------------------
    # 复卷纸追溯
    # ------------------------------------------------------------------

    def _roll_summary(row) -> RollSummary:
        return RollSummary(
            id=int(row["id"]),
            kind=row["kind"],
            length_mm=int(row["length_mm"]),
            inspection_id=(
                int(row["inspection_id"]) if row["inspection_id"] is not None else None
            ),
            note=row["note"],
            created_at=row["created_at"],
            splice_count=int(row["splice_count"]),
        )

    def _require_roll(connection: Connection, roll_id: int):
        row = db.get_roll(connection, roll_id)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"卷 {roll_id} 不存在",
            )
        return row

    @api.post(
        "/rolls/raw",
        response_model=RollSummary,
        status_code=status.HTTP_201_CREATED,
        tags=["卷追溯"],
        summary="登记原卷长度并关联已有质检登记号",
    )
    def create_raw_roll(
        payload: RawRollCreate, connection: Connection = Depends(get_conn)
    ) -> RollSummary:
        connection.execute("BEGIN IMMEDIATE")
        try:
            inspection = db.get_inspection(connection, payload.inspection_id)
            if inspection is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"质检登记号 {payload.inspection_id} 不存在，无法关联",
                )
            roll_id = db.create_raw_roll(
                connection,
                payload.length_mm,
                payload.inspection_id,
                payload.note,
            )
            connection.execute("COMMIT")
        except HTTPException:
            connection.execute("ROLLBACK")
            raise
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        return _roll_summary(_require_roll(connection, roll_id))

    @api.post(
        "/rolls/finished",
        response_model=RollDetail,
        status_code=status.HTTP_201_CREATED,
        tags=["卷追溯"],
        summary="登记成卷及按接入顺序的接段",
        description=(
            "接段按数组顺序接入成卷，记录来源卷、来源卷上的截取区间 [lo, hi) "
            "及是否掉头；来源卷可以是原卷，也可以是另一成卷。"
            "来源不存在、区间越界或来源链成环时整次拒收。"
        ),
    )
    def create_finished_roll(
        payload: FinishedRollCreate, connection: Connection = Depends(get_conn)
    ) -> RollDetail:
        plan = [
            (s.source_roll_id, s.source_lo, s.source_hi, s.reversed)
            for s in payload.splices
        ]
        connection.execute("BEGIN IMMEDIATE")
        try:
            rolls = db.load_rolls(connection)
            splices = db.load_splices(connection)
            roll_id = db.create_finished_roll(connection, payload.note)
            try:
                traceability.validate_finished_plan(rolls, splices, roll_id, plan)
            except TraceabilityError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"成卷登记被整次拒收：{exc}",
                ) from exc
            for seq, (source_id, source_lo, source_hi, reversed_flag) in enumerate(
                plan, start=1
            ):
                db.insert_splice(
                    connection,
                    roll_id,
                    seq,
                    source_id,
                    source_lo,
                    source_hi,
                    reversed_flag,
                )
            db.update_roll_length(connection, roll_id, traceability.finished_length(plan))
            connection.execute("COMMIT")
        except HTTPException:
            connection.execute("ROLLBACK")
            raise
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        row = _require_roll(connection, roll_id)
        return RollDetail(
            **_roll_summary(row).model_dump(),
            splices=[SpliceOut(**s) for s in db.list_splices(connection, roll_id)],
        )

    @api.get(
        "/rolls",
        response_model=list[RollSummary],
        tags=["卷追溯"],
        summary="列出全部原卷与成卷",
    )
    def list_rolls(connection: Connection = Depends(get_conn)) -> list[RollSummary]:
        return [_roll_summary(row) for row in db.list_rolls(connection)]

    @api.get(
        "/rolls/{roll_id}",
        response_model=RollDetail,
        tags=["卷追溯"],
        summary="查看卷信息与接段",
    )
    def get_roll(
        roll_id: int, connection: Connection = Depends(get_conn)
    ) -> RollDetail:
        row = _require_roll(connection, roll_id)
        return RollDetail(
            **_roll_summary(row).model_dump(),
            splices=[SpliceOut(**s) for s in db.list_splices(connection, roll_id)],
        )

    @api.patch(
        "/rolls/{roll_id}",
        response_model=RollSummary,
        tags=["卷追溯"],
        summary="更正原卷长度/备注，或成卷备注",
        description=(
            "原卷长度更正若使已登记的下游接段截取越界，整次拒收，原数据保持不变。"
            "成卷长度由接段决定，不接受 length_mm。"
        ),
    )
    def update_roll(
        roll_id: int,
        payload: RollUpdate,
        connection: Connection = Depends(get_conn),
    ) -> RollSummary:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = _require_roll(connection, roll_id)
            if row["kind"] == "raw":
                new_length = (
                    payload.length_mm
                    if "length_mm" in payload.model_fields_set
                    else int(row["length_mm"])
                )
                new_note = (
                    payload.note
                    if "note" in payload.model_fields_set
                    else row["note"]
                )
                if "length_mm" in payload.model_fields_set:
                    try:
                        traceability.validate_raw_length(
                            db.load_rolls(connection),
                            db.load_splices(connection),
                            roll_id,
                            new_length,
                        )
                    except TraceabilityError as exc:
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail=f"长度更正被整次拒收：{exc}",
                        ) from exc
                db.update_roll_length(connection, roll_id, new_length)
                db.update_roll_note(connection, roll_id, new_note)
            else:
                if "length_mm" in payload.model_fields_set:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="成卷长度由接段决定，仅支持更正备注（note）",
                    )
                new_note = (
                    payload.note
                    if "note" in payload.model_fields_set
                    else row["note"]
                )
                db.update_roll_note(connection, roll_id, new_note)
            connection.execute("COMMIT")
        except HTTPException:
            connection.execute("ROLLBACK")
            raise
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        return _roll_summary(_require_roll(connection, roll_id))

    @api.post(
        "/rolls/{roll_id}/splices",
        response_model=RollDetail,
        status_code=status.HTTP_201_CREATED,
        tags=["卷追溯"],
        summary="向成卷追加一个接段",
        description=(
            "按当前接入顺序追加到成卷末尾。来源不存在、区间越界、来源链成环，"
            "或使已登记的下游截取越界时，整次拒收。"
        ),
    )
    def append_splice(
        roll_id: int,
        payload: SpliceIn,
        connection: Connection = Depends(get_conn),
    ) -> RollDetail:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = _require_roll(connection, roll_id)
            if row["kind"] != "finished":
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"卷 {roll_id} 是原卷，只有成卷可以登记接段",
                )
            rolls = db.load_rolls(connection)
            splices = db.load_splices(connection)
            existing = splices.get(roll_id, [])
            plan = [
                (
                    int(s["source_roll_id"]),
                    int(s["source_lo"]),
                    int(s["source_hi"]),
                    bool(s["reversed"]),
                )
                for s in existing
            ]
            plan.append(
                (
                    payload.source_roll_id,
                    payload.source_lo,
                    payload.source_hi,
                    payload.reversed,
                )
            )
            try:
                traceability.validate_finished_plan(rolls, splices, roll_id, plan)
            except TraceabilityError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"接段追加被整次拒收：{exc}",
                ) from exc
            seq = db.next_splice_seq(connection, roll_id)
            db.insert_splice(
                connection,
                roll_id,
                seq,
                payload.source_roll_id,
                payload.source_lo,
                payload.source_hi,
                payload.reversed,
            )
            db.update_roll_length(connection, roll_id, traceability.finished_length(plan))
            connection.execute("COMMIT")
        except HTTPException:
            connection.execute("ROLLBACK")
            raise
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        row = _require_roll(connection, roll_id)
        return RollDetail(
            **_roll_summary(row).model_dump(),
            splices=[SpliceOut(**s) for s in db.list_splices(connection, roll_id)],
        )

    @api.patch(
        "/splices/{splice_id}",
        response_model=SpliceOut,
        tags=["卷追溯"],
        summary="更正成卷的一个接段",
        description=(
            "可更正来源卷、截取区间或掉头标记。若造成来源链循环，或使已登记的"
            "下游截取越界，整次拒收，原接段保持不变。"
        ),
    )
    def patch_splice(
        splice_id: int,
        payload: SplicePatch,
        connection: Connection = Depends(get_conn),
    ) -> SpliceOut:
        connection.execute("BEGIN IMMEDIATE")
        try:
            splice_row = db.get_splice(connection, splice_id)
            if splice_row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"接段 {splice_id} 不存在",
                )
            roll_id = int(splice_row["roll_id"])
            fields = payload.model_fields_set
            new_source_id = (
                payload.source_roll_id
                if "source_roll_id" in fields
                else int(splice_row["source_roll_id"])
            )
            new_lo = (
                payload.source_lo
                if "source_lo" in fields
                else int(splice_row["source_lo"])
            )
            new_hi = (
                payload.source_hi
                if "source_hi" in fields
                else int(splice_row["source_hi"])
            )
            new_reversed = (
                payload.reversed
                if "reversed" in fields
                else bool(splice_row["reversed"])
            )
            rolls = db.load_rolls(connection)
            splices = db.load_splices(connection)
            plan = [
                (
                    int(s["source_roll_id"]),
                    int(s["source_lo"]),
                    int(s["source_hi"]),
                    bool(s["reversed"]),
                )
                for s in splices.get(roll_id, [])
            ]
            plan[int(splice_row["seq"]) - 1] = (
                new_source_id,
                new_lo,
                new_hi,
                bool(new_reversed),
            )
            try:
                traceability.validate_finished_plan(rolls, splices, roll_id, plan)
            except TraceabilityError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"接段更正被整次拒收：{exc}",
                ) from exc
            db.update_splice(
                connection,
                splice_id,
                new_source_id,
                new_lo,
                new_hi,
                bool(new_reversed),
            )
            db.update_roll_length(connection, roll_id, traceability.finished_length(plan))
            connection.execute("COMMIT")
        except HTTPException:
            connection.execute("ROLLBACK")
            raise
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        for splice in db.list_splices(connection, roll_id):
            if splice["id"] == splice_id:
                return SpliceOut(**splice)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="接段更正后未能读回",
        )

    @api.get(
        "/rolls/{roll_id}/trace",
        response_model=TraceOut,
        tags=["卷追溯"],
        summary="追溯成卷一段的原卷来源与质检登记号",
        description=(
            "查询成卷上的 [lo, hi)（毫米，含起点不含终点），按成卷顺序返回每段"
            "对应的原卷号、原卷区间、方向（正向/反向，掉头奇数次为反向）和"
            "质检登记号；遇接缝自动分段。查询在单一数据库快照内完成，"
            "赶上并发更正时只会看到完整的旧结果或完整的新结果。"
        ),
    )
    def trace_roll(
        roll_id: int,
        lo: int = Query(..., ge=0, description="查询区间起点，毫米，包含"),
        hi: int = Query(..., description="查询区间终点，毫米，不包含，须大于起点"),
        connection: Connection = Depends(get_conn),
    ) -> TraceOut:
        if hi <= lo:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="hi 必须大于 lo（区间含起点、不含终点）",
            )
        # 显式只读事务：固定 WAL 快照，多表读取不会混入更正的中间状态。
        connection.execute("BEGIN")
        try:
            _require_roll(connection, roll_id)
            rolls = db.load_rolls(connection)
            splices = db.load_splices(connection)
            try:
                items = traceability.trace(rolls, splices, roll_id, lo, hi)
            except TraceabilityError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=str(exc),
                ) from exc
        finally:
            # 只读事务直接提交（无写入），结束快照。
            connection.execute("COMMIT")
        return TraceOut(roll_id=roll_id, query_lo=lo, query_hi=hi, items=items)

    return api


app = create_app()

"""复卷纸追溯的领域逻辑：接段校验、循环检测与递归追溯。

位置约定
--------
所有位置都从卷头起按毫米计，区间均为 ``[lo, hi)``——含起点、不含终点。
成卷的接段按 ``seq`` 顺序首尾相接：第 k 段在成卷上占据
``[prefix_k, prefix_k + source_hi - source_lo)``。

掉头（``reversed=True``）表示该段相对来源卷反向接入。成卷位置
``[a, b)`` 映射到来源卷时：

* 未掉头：``[source_lo + (a-start), source_lo + (b-start))``；
* 掉头：``[source_hi - (b-start), source_hi - (a-start))``。

沿接段链递归到原卷时，方向为沿途各段掉头标记的异或（奇数次掉头即反向）。
"""

from __future__ import annotations

from typing import Sequence

# 内存中的卷记录（由 db.load_rolls 载入）。
Roll = dict
# 内存中的接段记录（由 db.load_splices 载入）。
Splice = dict
# 待校验的接段方案：(来源卷 id, 来源区间起点, 来源区间终点, 是否掉头)。
PlanItem = tuple[int, int, int, bool]


class TraceabilityError(Exception):
    """追溯业务规则冲突（来源不存在、区间越界、成卷循环等）。

    由 API 层映射为相应的 4xx 响应；抛出时任何写入都尚未提交。
    """


def finished_length(plan: Sequence[PlanItem]) -> int:
    """成卷长度 = 各接段截取长度之和。"""
    return sum(hi - lo for _, lo, hi, _ in plan)


def seam_positions(splices: Sequence[Splice]) -> list[tuple[Splice, int, int]]:
    """按接入顺序返回 (接段, 成卷起点, 成卷终点)，终点不含。"""
    positions: list[tuple[Splice, int, int]] = []
    cursor = 0
    for splice in sorted(splices, key=lambda s: s["seq"]):
        length = int(splice["source_hi"]) - int(splice["source_lo"])
        positions.append((splice, cursor, cursor + length))
        cursor += length
    return positions


def _reachable_source_ids(
    rolls: dict[int, Roll],
    splices: dict[int, list[Splice]],
    owner_id: int,
    owner_plan: Sequence[PlanItem],
) -> set[int]:
    """从 owner_id 的来源沿接段链能到达的全部来源卷 id。

    ``owner_id`` 自身的出边使用假设方案 ``owner_plan``（尚未写入），
    其他成卷使用已登记的接段。返回集合若包含 ``owner_id``，即说明
    来源链回到了本卷，构成循环。
    """
    seen: set[int] = set()
    stack = [source_id for source_id, _, _, _ in owner_plan]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        if current == owner_id:
            edges = [item[0] for item in owner_plan]
        else:
            edges = [s["source_roll_id"] for s in splices.get(current, [])]
        for source_id in edges:
            if source_id not in seen:
                stack.append(source_id)
    return seen


def validate_finished_plan(
    rolls: dict[int, Roll],
    splices: dict[int, list[Splice]],
    owner_id: int,
    plan: Sequence[PlanItem],
) -> None:
    """校验成卷的整套接段方案（新建、追加、更正单个接段统一走这里）。

    检查项：

    1. 方案非空，每段来源卷存在且截取区间落在来源卷当前长度内；
    2. 方案不会使接段关系出现循环（含成卷直接/间接引用自己）；
    3. 方案不使任何**已登记的下游截取**越界——方案中引用的来源卷
       若缩短或换卷，已挂在它上面的接段必须仍在其长度内。

    任一不满足抛出 :class:`TraceabilityError`，整次写入由调用方拒收。
    """
    if not plan:
        raise TraceabilityError("成卷至少要有一个接段")

    for source_id, source_lo, source_hi, _ in plan:
        source = rolls.get(source_id)
        if source is None:
            raise TraceabilityError(f"来源卷 {source_id} 不存在")
        if source_lo < 0 or source_hi <= source_lo:
            raise TraceabilityError(
                f"来源卷 {source_id} 的截取区间 [{source_lo}, {source_hi}) 非法："
                "区间须含起点、不含终点且起点非负"
            )
        if source_hi > int(source["length_mm"]):
            raise TraceabilityError(
                f"截取区间 [{source_lo}, {source_hi}) 超出来源卷 {source_id} "
                f"的当前长度 {source['length_mm']} 毫米"
            )

    # 循环检测：从本卷的来源沿（假设）接段链能回到本卷即构成循环。
    reachable = _reachable_source_ids(rolls, splices, owner_id, plan)
    if owner_id in reachable:
        raise TraceabilityError(
            f"接段方案使成卷 {owner_id} 的来源链出现循环，整次拒收"
        )

    # 下游越界检测：方案引用到的每个来源卷，其已登记的下游接段
    # （属于其他成卷，或本卷方案之外的旧接段）都必须仍在长度内。
    referenced = {source_id for source_id, _, _, _ in plan}
    plan_by_source: dict[int, list[PlanItem]] = {}
    for item in plan:
        plan_by_source.setdefault(item[0], []).append(item)

    for source_id in referenced:
        source_length = int(rolls[source_id]["length_mm"])
        for splice_list in splices.values():
            for splice in splice_list:
                if int(splice["source_roll_id"]) != source_id:
                    continue
                # 本卷的旧接段已被 owner_plan 取代，以方案内的新值为准。
                if int(splice["roll_id"]) == owner_id:
                    continue
                if int(splice["source_hi"]) > source_length:
                    raise TraceabilityError(
                        f"成卷 {splice['roll_id']} 已登记的接段 "
                        f"[{splice['source_lo']}, {splice['source_hi']}) 会超出"
                        f"来源卷 {source_id} 的长度 {source_length} 毫米，"
                        "造成下游截取越界，整次拒收"
                    )
        # 本卷方案自身引用该来源卷的区间已在上面逐条校验过长度。
        for item in plan_by_source.get(source_id, []):
            if item[2] > source_length:  # 防御性分支，正常不会走到
                raise TraceabilityError(
                    f"接段区间超出来源卷 {source_id} 的长度 {source_length} 毫米"
                )


def validate_raw_length(
    rolls: dict[int, Roll],
    splices: dict[int, list[Splice]],
    roll_id: int,
    new_length: int,
) -> None:
    """校验原卷更正后的长度不使已登记的下游截取越界。"""
    if new_length <= 0:
        raise TraceabilityError("卷长必须为正整数（毫米）")
    for splice_list in splices.values():
        for splice in splice_list:
            if int(splice["source_roll_id"]) != roll_id:
                continue
            if int(splice["source_hi"]) > new_length:
                raise TraceabilityError(
                    f"成卷 {splice['roll_id']} 已登记的接段 "
                    f"[{splice['source_lo']}, {splice['source_hi']}) 会超出原卷 "
                    f"{roll_id} 更正后的长度 {new_length} 毫米，"
                    "造成下游截取越界，整次拒收"
                )


def _map_through_splice(
    splice: Splice, seg_start: int, a: int, b: int
) -> tuple[int, int]:
    """成卷上的 [a, b)（落在该接段内）映射为来源卷坐标。"""
    offset_a = a - seg_start
    offset_b = b - seg_start
    source_lo = int(splice["source_lo"])
    source_hi = int(splice["source_hi"])
    if not splice["reversed"]:
        return source_lo + offset_a, source_lo + offset_b
    # 掉头：成卷卷头方向对应来源卷的 source_hi 一侧。
    return source_hi - offset_b, source_hi - offset_a


def trace(
    rolls: dict[int, Roll],
    splices: dict[int, list[Splice]],
    roll_id: int,
    lo: int,
    hi: int,
) -> list[dict]:
    """追溯成卷 ``roll_id`` 上 ``[lo, hi)`` 这一段的原卷来源。

    返回按成卷顺序排列的分段结果；每穿过一个接缝就切一段。
    每个结果项含原卷号、原卷区间、方向（正向/反向）和质检登记号。
    """
    roll = rolls.get(roll_id)
    if roll is None:
        raise TraceabilityError(f"卷 {roll_id} 不存在")
    if roll["kind"] != "finished":
        raise TraceabilityError(f"卷 {roll_id} 是原卷，追溯查询仅支持成卷")
    if lo < 0 or hi <= lo:
        raise TraceabilityError(
            f"查询区间 [{lo}, {hi}) 非法：起点须非负，且终点大于起点"
        )
    if hi > int(roll["length_mm"]):
        raise TraceabilityError(
            f"查询区间 [{lo}, {hi}) 超出成卷 {roll_id} 的长度 "
            f"{roll['length_mm']} 毫米"
        )

    items: list[dict] = []
    visiting: set[int] = set()

    def resolve(
        current_id: int,
        cur_lo: int,
        cur_hi: int,
        flipped: bool,
        walk_reversed: bool,
    ) -> None:
        """递归追溯当前卷上的 [cur_lo, cur_hi)。

        ``flipped`` 累积沿途掉头次数的奇偶，决定叶子（原卷）的方向；
        ``walk_reversed`` 表示查询的行走方向是否与当前卷的卷头方向相反
        （即从卷尾走向卷头）——掉头接段指向多接段成卷时，子接段必须
        反序访问，输出顺序才与查询卷上的物理顺序一致。
        """
        current = rolls[current_id]
        if current["kind"] == "raw":
            items.append(
                {
                    "raw_roll_id": current_id,
                    "inspection_id": int(current["inspection_id"]),
                    "raw_lo": cur_lo,
                    "raw_hi": cur_hi,
                    "direction": "反向" if flipped else "正向",
                    "reversed": flipped,
                }
            )
            return

        if current_id in visiting:  # 防御：写入路径已保证无循环
            raise TraceabilityError(
                f"成卷 {current_id} 的来源链存在循环，无法追溯"
            )
        visiting.add(current_id)
        try:
            positioned = seam_positions(splices.get(current_id, []))
            if walk_reversed:
                positioned = list(reversed(positioned))
            for splice, seg_start, seg_end in positioned:
                overlap_lo = max(cur_lo, seg_start)
                overlap_hi = min(cur_hi, seg_end)
                if overlap_lo >= overlap_hi:
                    continue
                source_lo, source_hi = _map_through_splice(
                    splice, seg_start, overlap_lo, overlap_hi
                )
                splice_reversed = bool(splice["reversed"])
                resolve(
                    int(splice["source_roll_id"]),
                    source_lo,
                    source_hi,
                    flipped ^ splice_reversed,
                    walk_reversed ^ splice_reversed,
                )
        finally:
            visiting.discard(current_id)

    # 顶层查询沿成卷卷头 -> 卷尾行走。
    resolve(roll_id, lo, hi, False, False)
    return items

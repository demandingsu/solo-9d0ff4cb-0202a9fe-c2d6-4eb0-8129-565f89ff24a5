"""追溯并发测试：写事务串行化，读走快照，查询不会看到更正中间态。"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app  # noqa: E402


def _build_chain(client):
    # 质检登记 + 两个原卷 + 一个跨接缝成卷
    insp = client.post("/inspections", json={"total_pieces": 10}).json()["id"]
    client.post(
        "/rolls/raw", json={"length_mm": 1000, "inspection_id": insp}
    )
    client.post(
        "/rolls/raw", json={"length_mm": 1000, "inspection_id": insp}
    )
    client.post(
        "/rolls/finished",
        json={
            "splices": [
                {"source_roll_id": 1, "source_lo": 0, "source_hi": 300, "reversed": False},
                {"source_roll_id": 2, "source_lo": 0, "source_hi": 500, "reversed": False},
            ]
        },
    )


def test_trace_concurrent_with_correction_always_sees_consistent_state(tmp_path):
    app = create_app(str(tmp_path / "trace_concurrent.db"))
    with TestClient(app) as client:
        _build_chain(client)

        def correct(_):
            # 交替把原卷 1 的长度在 1000 / 1200 之间更正（都不造成下游越界）
            length = 1200 if _ % 2 == 0 else 1000
            resp = client.patch("/rolls/1", json={"length_mm": length})
            assert resp.status_code == 200, resp.text
            return length

        def query(_):
            resp = client.get("/rolls/3/trace", params={"lo": 0, "hi": 800})
            assert resp.status_code == 200, resp.text
            body = resp.json()
            items = body["items"]
            # 完整结果的不变量：两段、覆盖全长、顺序固定、字段齐全
            assert [item["raw_roll_id"] for item in items] == [1, 2]
            assert (
                sum(item["raw_hi"] - item["raw_lo"] for item in items) == 800
            )
            for item in items:
                assert item["inspection_id"] == 1
                assert item["direction"] in ("正向", "反向")
                assert item["raw_hi"] > item["raw_lo"]
            return items

        with ThreadPoolExecutor(max_workers=4) as pool:
            corrections = list(pool.map(correct, range(20)))
            queries = list(pool.map(query, range(40)))

        assert len(corrections) == 20
        assert len(queries) == 40
        # 并发结束后做一次确定性更正，固定最终状态
        final = client.patch("/rolls/1", json={"length_mm": 1000})
        assert final.status_code == 200
        assert client.get("/rolls/1").json()["length_mm"] == 1000


def test_concurrent_compatible_splices_both_accepted(tmp_path):
    app = create_app(str(tmp_path / "trace_concurrent2.db"))
    with TestClient(app) as client:
        _build_chain(client)

        def append(source_id):
            return client.post(
                "/rolls/3/splices",
                json={
                    "source_roll_id": source_id,
                    "source_lo": 0,
                    "source_hi": 10,
                    "reversed": False,
                },
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(append, [1, 2]))

        assert sorted(statuses) == [201, 201]
        detail = client.get("/rolls/3").json()
        assert detail["splice_count"] == 4
        # 两个并发事务串行化后，seq 与长度一致
        assert [s["seq"] for s in detail["splices"]] == [1, 2, 3, 4]
        assert detail["length_mm"] == 820


def test_concurrent_conflicting_raw_length_only_one_wins(tmp_path):
    app = create_app(str(tmp_path / "trace_concurrent3.db"))
    with TestClient(app) as client:
        _build_chain(client)
        # 成卷 3 已登记截取原卷 1 的 [0,300)：长度 200 会越界，长度 900 合法
        def patch(length):
            return client.patch("/rolls/1", json={"length_mm": length}).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(patch, [200, 900]))

        assert sorted(statuses) == [200, 409]
        assert client.get("/rolls/1").json()["length_mm"] == 900
        # 拒收后追溯仍可用且结果完整
        items = client.get("/rolls/3/trace", params={"lo": 0, "hi": 800}).json()[
            "items"
        ]
        assert len(items) == 2

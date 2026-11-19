"""复卷纸追溯 API 测试。

覆盖：
* 原卷登记（关联质检登记号、未知登记号拒收）
* 成卷登记与追加接段、成卷长度自动计算、接段位置
* 跨接缝追溯分段、来源为另一成卷、两次掉头方向还原
* 查询参数与越界校验
* 循环拒收（直接自引用、间接成环）
* 更正原卷长度/接段导致下游越界时整次拒收、原数据不变
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "trace.db"))
    with TestClient(app) as test_client:
        yield test_client


def make_inspection(client, total_pieces=100):
    response = client.post("/inspections", json={"total_pieces": total_pieces})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def make_raw(client, length_mm, inspection_id=None, note=None):
    if inspection_id is None:
        inspection_id = make_inspection(client)
    response = client.post(
        "/rolls/raw",
        json={
            "length_mm": length_mm,
            "inspection_id": inspection_id,
            "note": note,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_finished(client, splices, note=None):
    response = client.post(
        "/rolls/finished",
        json={"note": note, "splices": splices},
    )
    assert response.status_code == 201, response.text
    return response.json()


def splice(source_roll_id, source_lo, source_hi, reversed_flag=False):
    return {
        "source_roll_id": source_roll_id,
        "source_lo": source_lo,
        "source_hi": source_hi,
        "reversed": reversed_flag,
    }


# ---------------------------------------------------------------------------
# 登记
# ---------------------------------------------------------------------------


def test_raw_roll_registration_links_inspection(client):
    inspection_id = make_inspection(client, 100)
    raw = make_raw(client, 1000, inspection_id=inspection_id, note="原卷 A")
    assert raw == {
        "id": 1,
        "kind": "raw",
        "length_mm": 1000,
        "inspection_id": inspection_id,
        "note": "原卷 A",
        "created_at": raw["created_at"],
        "splice_count": 0,
    }

    listed = client.get("/rolls").json()
    assert len(listed) == 1
    assert listed[0]["inspection_id"] == inspection_id


def test_raw_roll_unknown_inspection_is_404(client):
    response = client.post(
        "/rolls/raw",
        json={"length_mm": 1000, "inspection_id": 999},
    )
    assert response.status_code == 404
    assert client.get("/rolls").json() == []


def test_raw_roll_validation(client):
    inspection_id = make_inspection(client)
    bad_length = client.post(
        "/rolls/raw",
        json={"length_mm": 0, "inspection_id": inspection_id},
    )
    assert bad_length.status_code == 422
    missing = client.post("/rolls/raw", json={"length_mm": 100})
    assert missing.status_code == 422


def test_finished_roll_registration_computes_length_and_positions(client):
    make_raw(client, 1000)  # roll 1
    make_raw(client, 2000)  # roll 2
    finished = make_finished(
        client,
        [splice(1, 0, 300), splice(2, 100, 600, reversed_flag=True)],
    )
    assert finished["id"] == 3
    assert finished["kind"] == "finished"
    assert finished["length_mm"] == 800  # 300 + 500
    assert finished["inspection_id"] is None
    assert finished["splice_count"] == 2
    assert [
        (s["seq"], s["start"], s["end"], s["reversed"]) for s in finished["splices"]
    ] == [(1, 0, 300, False), (2, 300, 800, True)]


def test_finished_roll_rejects_out_of_range_and_missing_source(client):
    make_raw(client, 100)  # roll 1
    response = client.post(
        "/rolls/finished",
        json={"splices": [splice(1, 50, 150)]},
    )
    assert response.status_code == 409
    assert "拒收" in response.json()["detail"]

    response = client.post(
        "/rolls/finished",
        json={"splices": [splice(99, 0, 10)]},
    )
    assert response.status_code == 409
    # 两次拒收都没有留下半成品，只剩原卷
    listed = client.get("/rolls").json()
    assert len(listed) == 1
    assert listed[0]["id"] == 1


def test_finished_roll_requires_splices(client):
    response = client.post("/rolls/finished", json={"splices": []})
    assert response.status_code == 422
    bad = client.post(
        "/rolls/finished",
        json={"splices": [splice(1, 100, 50)]},
    )
    assert bad.status_code == 422  # hi <= lo 在 schema 层拦截


def test_append_splice_extends_roll(client):
    make_raw(client, 1000)
    make_raw(client, 1000)
    make_finished(client, [splice(1, 0, 200)])  # roll 3, length 200

    response = client.post("/rolls/3/splices", json=splice(2, 50, 250))
    assert response.status_code == 201, response.text
    detail = response.json()
    assert detail["length_mm"] == 400
    assert [s["seq"] for s in detail["splices"]] == [1, 2]
    assert detail["splices"][1]["start"] == 200


def test_append_to_raw_roll_rejected(client):
    make_raw(client, 1000)
    response = client.post("/rolls/1/splices", json=splice(1, 0, 10))
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# 追溯：跨接缝、成卷来源、掉头
# ---------------------------------------------------------------------------


def test_trace_across_seams_splits_results(client):
    make_raw(client, 1000)  # 1: 原卷
    make_raw(client, 1000)  # 2: 原卷
    make_finished(
        client,
        [splice(1, 0, 300), splice(2, 100, 600)],
    )  # 3: 成卷 [0,300)+[300,800)

    # 查询跨越 300 毫米处的接缝
    response = client.get("/rolls/3/trace", params={"lo": 200, "hi": 400})
    assert response.status_code == 200
    assert response.json() == {
        "roll_id": 3,
        "query_lo": 200,
        "query_hi": 400,
        "items": [
            {
                "raw_roll_id": 1,
                "inspection_id": 1,
                "raw_lo": 200,
                "raw_hi": 300,
                "direction": "正向",
                "reversed": False,
            },
            {
                "raw_roll_id": 2,
                "inspection_id": 2,
                "raw_lo": 100,
                "raw_hi": 200,
                "direction": "正向",
                "reversed": False,
            },
        ],
    }


def test_trace_seam_aligned_query_and_full_roll(client):
    make_raw(client, 1000)
    make_raw(client, 1000)
    make_finished(client, [splice(1, 0, 300), splice(2, 0, 500)])

    # 端点正好落在接缝上：不产生空段
    response = client.get("/rolls/3/trace", params={"lo": 300, "hi": 800})
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["raw_roll_id"] == 2
    assert (items[0]["raw_lo"], items[0]["raw_hi"]) == (0, 500)

    # 整卷追溯覆盖全部长度
    full = client.get("/rolls/3/trace", params={"lo": 0, "hi": 800}).json()
    assert sum(i["raw_hi"] - i["raw_lo"] for i in full["items"]) == 800


def test_trace_single_reversal_maps_coordinates(client):
    make_raw(client, 1000)  # roll 1
    make_finished(client, [splice(1, 100, 500, reversed_flag=True)])  # roll 2

    # 成卷 [0,100) 对应掉头后的来源 [400,500)
    response = client.get("/rolls/2/trace", params={"lo": 0, "hi": 100})
    item = response.json()["items"][0]
    assert item["raw_roll_id"] == 1
    assert (item["raw_lo"], item["raw_hi"]) == (400, 500)
    assert item["direction"] == "反向"
    assert item["reversed"] is True

    # 成卷 [300,400) 对应来源 [100,200)
    response = client.get("/rolls/2/trace", params={"lo": 300, "hi": 400})
    item = response.json()["items"][0]
    assert (item["raw_lo"], item["raw_hi"]) == (100, 200)
    assert item["direction"] == "反向"


def test_trace_through_finished_source_with_two_reversals(client):
    # 原卷 1 长 1000；成卷 2 掉头取原卷 [100,500)（长 400）
    make_raw(client, 1000)
    make_finished(client, [splice(1, 100, 500, reversed_flag=True)])
    # 成卷 3 再掉头取成卷 2 的 [0,200)：两次掉头 => 相对原卷为正向
    make_finished(client, [splice(2, 0, 200, reversed_flag=True)])

    response = client.get("/rolls/3/trace", params={"lo": 0, "hi": 200})
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    item = items[0]
    # 成卷2[0,200) 掉头 => 成卷2坐标其实映射原卷 [300,500)（见上例逻辑）；
    # 再掉头一次 => 原卷 [300,500)，方向正向。
    assert item["raw_roll_id"] == 1
    assert (item["raw_lo"], item["raw_hi"]) == (300, 500)
    assert item["direction"] == "正向"
    assert item["reversed"] is False
    assert item["inspection_id"] == 1


def test_trace_mixed_directions_across_chain(client):
    # 原卷 1、2；成卷 3 = 原卷1 正向 [0,100) + 原卷2 掉头 [0,100)
    make_raw(client, 500)
    make_raw(client, 500)
    make_finished(
        client,
        [splice(1, 0, 100), splice(2, 0, 100, reversed_flag=True)],
    )
    # 成卷 4 掉头接入成卷 3 的整段 => 两段方向全部翻转，顺序仍按成卷4排列
    make_finished(client, [splice(3, 0, 200, reversed_flag=True)])

    response = client.get("/rolls/4/trace", params={"lo": 0, "hi": 200})
    items = response.json()["items"]
    assert len(items) == 2
    # 成卷4[0,100) 掉头 => 成卷3[100,200)；该段在成卷3中是原卷2掉头 [0,100)，
    # 再经一次掉头 => 原卷2 正向 [0,100)。
    assert items[0]["raw_roll_id"] == 2
    assert items[0]["direction"] == "正向"
    assert (items[0]["raw_lo"], items[0]["raw_hi"]) == (0, 100)
    # 成卷4[100,200) 掉头 => 成卷3[0,100)；该段在成卷3中是原卷1正向 [0,100)，
    # 经成卷4一次掉头 => 原卷1 反向 [0,100)。
    assert items[1]["raw_roll_id"] == 1
    assert items[1]["direction"] == "反向"
    assert (items[1]["raw_lo"], items[1]["raw_hi"]) == (0, 100)


def test_trace_validation_and_not_found(client):
    make_raw(client, 1000)
    make_finished(client, [splice(1, 0, 100)])  # roll 2, length 100

    assert client.get("/rolls/99/trace", params={"lo": 0, "hi": 10}).status_code == 404
    # 原卷不支持追溯查询
    assert client.get("/rolls/1/trace", params={"lo": 0, "hi": 10}).status_code == 409
    # hi <= lo
    bad = client.get("/rolls/2/trace", params={"lo": 50, "hi": 50})
    assert bad.status_code == 422
    # 负起点
    assert client.get("/rolls/2/trace", params={"lo": -1, "hi": 10}).status_code == 422
    # 超出成卷长度
    over = client.get("/rolls/2/trace", params={"lo": 0, "hi": 101})
    assert over.status_code == 409
    assert "超出" in over.json()["detail"]


# ---------------------------------------------------------------------------
# 循环与更正冲突
# ---------------------------------------------------------------------------


def test_finished_roll_self_reference_is_rejected(client):
    make_raw(client, 1000)
    make_finished(client, [splice(1, 0, 100)])  # roll 2
    # 追加接段时引用自己 => 循环
    response = client.post("/rolls/2/splices", json=splice(2, 0, 50))
    assert response.status_code == 409
    assert "循环" in response.json()["detail"]
    # 原数据不变
    detail = client.get("/rolls/2").json()
    assert detail["splice_count"] == 1
    assert detail["length_mm"] == 100


def test_indirect_cycle_via_splice_patch_is_rejected(client):
    make_raw(client, 1000)  # 1
    make_finished(client, [splice(1, 0, 100)])  # 2
    make_finished(client, [splice(2, 0, 100)])  # 3 引用 2
    splice_id = client.get("/rolls/2").json()["splices"][0]["id"]

    # 把成卷 2 的接段来源改成成卷 3 => 2 -> 3 -> 2 成环
    response = client.patch(
        f"/splices/{splice_id}",
        json={"source_roll_id": 3, "source_lo": 0, "source_hi": 100},
    )
    assert response.status_code == 409
    assert "循环" in response.json()["detail"]
    # 原接段保持不变
    unchanged = client.get("/rolls/2").json()["splices"][0]
    assert unchanged["source_roll_id"] == 1
    assert (unchanged["source_lo"], unchanged["source_hi"]) == (0, 100)


def test_new_roll_missing_source_is_rejected_entirely(client):
    make_raw(client, 1000)  # 1
    make_finished(client, [splice(1, 0, 100)])  # 2
    # 新建成卷时引用尚不存在的来源 id => 整份拒收，不留半成品
    response = client.post(
        "/rolls/finished",
        json={"splices": [splice(2, 0, 50), splice(99, 0, 10)]},
    )
    assert response.status_code == 409
    assert len(client.get("/rolls").json()) == 2


def test_indirect_cycle_on_append_is_rejected(client):
    make_raw(client, 1000)  # 1
    make_finished(client, [splice(1, 0, 100)])  # 2
    make_finished(client, [splice(2, 0, 100)])  # 3 -> 2
    # 让 2 追加一个引用 3 的接段 => 2 -> 3 -> 2 成环
    response = client.post("/rolls/2/splices", json=splice(3, 0, 50))
    assert response.status_code == 409
    assert "循环" in response.json()["detail"]
    detail = client.get("/rolls/2").json()
    assert detail["splice_count"] == 1
    assert detail["length_mm"] == 100
    # 成卷 3 也不受影响
    assert client.get("/rolls/3").json()["length_mm"] == 100


def test_patch_raw_length_causing_downstream_overflow_is_rejected(client):
    make_raw(client, 1000)  # 1
    make_finished(client, [splice(1, 200, 800)])  # 2，截取到 800

    before = client.get("/rolls/1").json()
    # 缩短到 700：已登记接段 [200,800) 越界
    conflict = client.patch("/rolls/1", json={"length_mm": 700})
    assert conflict.status_code == 409
    assert "越界" in conflict.json()["detail"]
    after = client.get("/rolls/1").json()
    assert after == before  # 整次拒收，原长度不变

    # 放宽到 1200 可以接收
    ok = client.patch("/rolls/1", json={"length_mm": 1200})
    assert ok.status_code == 200
    assert ok.json()["length_mm"] == 1200
    # 下游追溯照常工作
    trace = client.get("/rolls/2/trace", params={"lo": 0, "hi": 600}).json()
    assert trace["items"][0]["raw_roll_id"] == 1


def test_patch_splice_causing_overflow_is_rejected(client):
    make_raw(client, 100)  # 1，长 100
    make_raw(client, 1000)  # 2
    make_finished(client, [splice(1, 0, 100)])  # 3
    splice_id = client.get("/rolls/3").json()["splices"][0]["id"]

    # 把区间改到原卷 1 的长度之外 => 拒收
    response = client.patch(
        f"/splices/{splice_id}", json={"source_lo": 50, "source_hi": 150}
    )
    assert response.status_code == 409
    assert "超出" in response.json()["detail"]
    unchanged = client.get("/rolls/3").json()
    assert unchanged["length_mm"] == 100
    assert unchanged["splices"][0]["source_hi"] == 100

    # 合法更正（换来源 + 掉头）可以接收，成卷长度随之变化
    ok = client.patch(
        f"/splices/{splice_id}",
        json={
            "source_roll_id": 2,
            "source_lo": 0,
            "source_hi": 250,
            "reversed": True,
        },
    )
    assert ok.status_code == 200
    detail = client.get("/rolls/3").json()
    assert detail["length_mm"] == 250
    assert detail["splices"][0]["reversed"] is True


def test_patch_splice_not_found_and_empty_patch(client):
    make_raw(client, 1000)
    make_finished(client, [splice(1, 0, 100)])
    assert client.patch("/splices/999", json={"reversed": True}).status_code == 404
    splice_id = client.get("/rolls/2").json()["splices"][0]["id"]
    assert client.patch(f"/splices/{splice_id}", json={}).status_code == 422


def test_finished_roll_patch_only_note(client):
    make_raw(client, 1000)
    make_finished(client, [splice(1, 0, 100)], note="旧")
    ok = client.patch("/rolls/2", json={"note": "新备注"})
    assert ok.status_code == 200
    assert ok.json()["note"] == "新备注"
    # 成卷不接受 length_mm 字段（extra forbid + 联合类型）
    bad = client.patch("/rolls/2", json={"length_mm": 999})
    assert bad.status_code == 422


def test_raw_roll_patch_note_to_null(client):
    inspection_id = make_inspection(client)
    make_raw(client, 100, inspection_id=inspection_id, note="备注")
    response = client.patch("/rolls/1", json={"note": None})
    assert response.status_code == 200
    assert response.json()["note"] is None


def test_trace_returns_inspection_id_from_raw_registration(client):
    inspection_id = make_inspection(client, 42)
    make_raw(client, 500, inspection_id=inspection_id)
    make_finished(client, [splice(1, 10, 90)])
    items = client.get("/rolls/2/trace", params={"lo": 0, "hi": 80}).json()["items"]
    assert items[0]["inspection_id"] == inspection_id

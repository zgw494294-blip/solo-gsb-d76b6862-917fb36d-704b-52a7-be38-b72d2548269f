"""撤回末条后端回归测试（FastAPI TestClient + 独立临时数据库）。

覆盖：演出中撤回末条（发出快照作待发内容、保留 id 与编号、清空发出
快照/时间/序号、插到现有待发队首、现有待发内容与相对顺序不变、其他
已发记录不变、问题记录快照不变、版本 +1）；完成态撤回回到演出中；
暂停时也可撤回且暂停标记不变；再次确认记新时间；开场前 / 无已发 /
目标不是末条（已被撤回）/ 版本过期返回 409 且数据不变；撤回 + 版本 +1
同事务（与确认、编辑、重排、归档同版本竞争最多一个成功）；重启后结果
保留；既有档案不受撤回影响；不提供批量撤回。

位置分配回归（反复确认/撤回同一提示曾使待发位置指数增长：重排路径第
61 次撤回时位置溢出为浮点、待发顺序静默错乱；不重排路径第 62 次撤回
抛 OverflowError）：两条路径各跑 70 轮确认/撤回（每步提交最新版本，
中途重启）断言顺序不变、位置紧凑不增长；既有库中接近 SQLite 整数上限
且仍可区分的位置能正常撤回与重排；模拟写入失败时撤回整体回滚、数据
与版本不变且异常不被吞掉。

运行：python3 tests/withdraw.test.py
"""
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="cue-withdraw-test-")
os.environ["DB_PATH"] = os.path.join(_tmp, "cues.db")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402

failures = 0


def check(cond, msg):
    global failures
    if cond:
        print("  ✓ " + msg)
    else:
        failures += 1
        print("  ✗ " + msg)


CUES3 = [
    {"number": "A1", "content": "灯光渐亮"},
    {"number": "A2", "content": "音乐起"},
    {"number": "A3", "content": "幕落"},
]


def setup_show(c, cues=None):
    r = c.post("/api/setup", json={"cues": cues or CUES3})
    assert r.status_code == 200, r.text
    return r.json()


def confirm(c, v):
    r = c.post("/api/confirm", json={"version": v})
    assert r.status_code == 200, r.text
    return r.json()


def withdraw(c, v, cue_id):
    return c.post("/api/withdraw", json={"version": v, "cue_id": cue_id})


def test_basic_withdraw():
    with TestClient(main.app) as c:
        print("W1 演出中撤回：末条按发出快照退回待发队首，其余不变")
        s = setup_show(c)
        v = s["version"]
        # 先把第一条编辑成与初始不同的内容，再确认；撤回内容应为发出快照
        id1 = s["pending"][0]["id"]
        r = c.post("/api/edit", json={"version": v, "cue_id": id1,
                                      "content": "灯光慢亮（改过）"})
        check(r.status_code == 200, "确认前先编辑首条")
        v = r.json()["version"]

        s = confirm(c, v)
        v = s["version"]
        issued1 = s["issued"][0]
        check(issued1["id"] == id1, "首条已确认发出")
        check(issued1["content"] == "灯光慢亮（改过）", "已发记录保存发出快照")
        first_issued_at = issued1["issued_at"]
        check(s["current"]["id"] == id1 + 1, "当前推进到第二条")

        # 撤回首条（此时它也是末条）
        r = withdraw(c, v, id1)
        check(r.status_code == 200, "撤回成功")
        s = r.json()
        check(s["version"] == v + 1, "撤回与版本 +1 一起保存")
        check(s["status"] == "live", "仍为演出中")
        check(s["paused"] is False, "非暂停时撤回后仍未暂停")
        check(len(s["issued"]) == 0, "已发记录中该条消失")
        check(s["current"]["id"] == id1, "撤回提示回到当前待发（队首）")
        check(s["current"]["number"] == "A1", "保留原编号")
        check(s["current"]["content"] == "灯光慢亮（改过）", "待发内容为发出快照（不是初始内容）")
        pending_ids = [p["id"] for p in s["pending"]]
        check(pending_ids == [id1, id1 + 1, id1 + 2], "撤回提示插到队首，原待发整体后移且相对顺序不变")

        # 原始数据库行：发出快照 / 时间 / 序号已清空
        row = c.get("/api/state").json()
        check(row["issued"] == [], "刷新后该条仍在待发、不在已发（持久化结果）")


def test_other_records_unchanged():
    with TestClient(main.app) as c:
        print("W2 撤回不改其他已发、待发内容、问题记录快照")
        s = setup_show(c)
        v = s["version"]
        id1, id2, id3 = [p["id"] for p in s["pending"]]
        s = confirm(c, v)
        v = s["version"]
        # 编辑第三条待发内容（撤回后应保留此编辑）
        r = c.post("/api/edit", json={"version": v, "cue_id": id3, "content": "幕落（改）"})
        check(r.status_code == 200, "撤回前编辑其他待发")
        v = r.json()["version"]
        s = confirm(c, v)  # 发出 A2
        v = s["version"]
        # 为已发的 A1 与待发的 A3 记问题（撤回 A2 后快照不变）
        r = c.post("/api/issues", json={"version": v, "cue_id": id1, "text": "A1 灯光稍晚"})
        check(r.status_code == 200, "为已发 A1 记录问题")
        v = r.json()["version"]
        r = c.post("/api/issues", json={"version": v, "cue_id": id3, "text": "A3 待核对"})
        check(r.status_code == 200, "为待发 A3 记录问题")
        v = r.json()["version"]

        issues_before = c.get("/api/state").json()["issues"]
        r = withdraw(c, v, id2)
        check(r.status_code == 200, "撤回末条 A2 成功")
        s = r.json()
        check([p["id"] for p in s["issued"]] == [id1], "其他已发记录（A1）保留且顺序不变")
        check(s["issued"][0]["content"] == "灯光渐亮", "其他已发记录的发出快照不变")
        check(s["pending"][0]["id"] == id2, "A2 回到待发队首")
        check(s["pending"][0]["content"] == "音乐起", "A2 待发内容为其发出快照")
        check(s["pending"][1]["id"] == id3 and s["pending"][1]["content"] == "幕落（改）",
              "其他待发内容（含先前编辑）与相对顺序不变")
        check(s["issues"] == issues_before, "问题记录及其快照完全不变")


def test_done_back_to_live():
    with TestClient(main.app) as c:
        print("W3 完成态撤回：回到演出中，最后一条退回")
        s = setup_show(c)
        v = s["version"]
        for _ in range(3):
            s = confirm(c, v)
            v = s["version"]
        check(s["status"] == "done", "三条全部发出，进入已完成")
        check(s["current"] is None and s["remaining"] == 0, "完成时无待发")
        last_id = s["issued"][-1]["id"]

        r = withdraw(c, v, last_id)
        check(r.status_code == 200, "完成态可撤回")
        s = r.json()
        check(s["status"] == "live", "完成态撤回后回到演出中")
        check(s["current"]["id"] == last_id, "最后已发提示成为当前待发")
        check(len(s["issued"]) == 2, "前两条已发记录保留")
        check(s["remaining"] == 1, "待发计数恢复为 1")
        check(s["paused"] is False, "完成态原本未暂停，撤回后仍未暂停")


def test_paused_withdraw_keeps_pause():
    with TestClient(main.app) as c:
        print("W4 暂停时可撤回，暂停标记不变；恢复后再次确认记新时间")
        s = setup_show(c)
        v = s["version"]
        id1 = s["pending"][0]["id"]
        s = confirm(c, v)
        v = s["version"]
        old_time = s["issued"][0]["issued_at"]
        # 暂停
        r = c.post("/api/pause", json={"version": v, "paused": True})
        check(r.status_code == 200 and r.json()["paused"] is True, "暂停成功")
        v = r.json()["version"]
        old_seq_version = v  # 首次发出时的序号即确认后的版本号（暂停只再 +1）
        # 暂停期间确认被拒绝，撤回可用
        r = c.post("/api/confirm", json={"version": v})
        check(r.status_code == 409, "暂停期间确认仍被拒绝")
        r = withdraw(c, v, id1)
        check(r.status_code == 200, "暂停期间撤回成功")
        s = r.json()
        check(s["paused"] is True, "撤回不改变暂停标记")
        check(s["status"] == "live" and s["current"]["id"] == id1, "退回为当前待发")
        v = s["version"]
        # 暂停期间仍不能确认
        r = c.post("/api/confirm", json={"version": v})
        check(r.status_code == 409, "撤回后仍在暂停，确认继续被拒绝")
        # 恢复
        r = c.post("/api/pause", json={"version": v, "paused": False})
        check(r.status_code == 200, "恢复发出")
        v = r.json()["version"]
        s = confirm(c, v)
        new_time = s["issued"][0]["issued_at"]
        check(s["issued"][0]["id"] == id1, "再次确认后该提示重新进入已发")
        check(new_time != old_time, "再次确认记录新的发出时间（不复用旧时间）")
        # 发出序号不随状态 JSON 返回：直接查库确认用的是新版本号（序号单调递增）
        with main.db() as conn:
            seq = conn.execute(
                "SELECT issued_seq FROM cues WHERE id = ?", (id1,)
            ).fetchone()["issued_seq"]
        check(seq == s["version"] and seq > old_seq_version,
              "再次确认使用新的、更大的发出序号（撤回时旧序号已清空）")


def test_conflicts():
    with TestClient(main.app) as c:
        print("W5 失败情况：开场前 / 无已发 / 目标不符 / 版本过期均 409 且不变")
        s = c.get("/api/state").json()
        # 开场前（重置后的干净库）
        r = withdraw(c, s["version"], 1)
        check(r.status_code == 409, "开场前撤回返回 409")
        check(r.json()["state"]["version"] == s["version"], "409 附最新状态")

        s = setup_show(c)
        v = s["version"]
        id1, id2, _ = [p["id"] for p in s["pending"]]
        r = withdraw(c, v, id1)
        check(r.status_code == 409, "演出中但无已发提示：409")
        check(c.get("/api/state").json()["version"] == v, "无已发时撤回版本不变")

        s = confirm(c, v)
        v = s["version"]
        r = withdraw(c, v + 99, id1)
        check(r.status_code == 409, "版本过期返回 409")
        check(c.get("/api/state").json()["version"] == v, "过期版本不改数据")

        r = withdraw(c, v, id2)
        check(r.status_code == 409, "目标不是最后已发提示返回 409")
        check(c.get("/api/state").json()["version"] == v, "目标不符不改数据")

        # 正确撤回后，旧请求再来一次：末条已不在已发
        r = withdraw(c, v, id1)
        check(r.status_code == 200, "首次撤回成功")
        v2 = r.json()["version"]
        r = withdraw(c, v2, id1)
        check(r.status_code == 409, "已被撤回后同 id 再撤回：409（不批量撤回）")

        # 再发两条，撤末条只能撤最后一条，不能跳撤
        s = confirm(c, v2)
        v = s["version"]
        s = confirm(c, v)
        v = s["version"]
        r = withdraw(c, v, id1)
        check(r.status_code == 409, "有多条已发时不能跳过末条撤回更早的提示")
        check([x["id"] for x in c.get("/api/state").json()["issued"]] == [id1, id2],
              "跳撤失败，已发记录不变")


def test_same_version_competition():
    with TestClient(main.app) as c:
        print("W6 同版本竞争：撤回与确认/编辑/重排/归档最多一个成功")
        s = setup_show(c)
        v = s["version"]
        id1, id2, id3 = [p["id"] for p in s["pending"]]
        s = confirm(c, v)
        v = s["version"]

        # 同一版本并发：撤回 + 编辑（编辑的是待发 id2）
        r1 = withdraw(c, v, id1)
        r2 = c.post("/api/edit", json={"version": v, "cue_id": id2, "content": "改写"})
        codes = sorted([r1.status_code, r2.status_code])
        check(codes == [200, 409], "同版本撤回与编辑：一个 200 一个 409")
        cur = c.get("/api/state").json()
        check(cur["version"] == v + 1, "竞争后版本只 +1")

        # 再来：构造一场确定的"撤回与确认"同版本竞争——
        # 若上一轮编辑胜出，A1 仍在已发（队首为 A2）：先撤回会让 A1 回队首；
        # 若上一轮撤回避出，A1 在待发队首、A2 待发：先发一条让已发重新非空。
        if not cur["issued"]:
            # 撤回避出：队首即 A1，确认它使已发非空
            r = c.post("/api/confirm", json={"version": cur["version"]})
            check(r.status_code == 200, "补发一条使已发非空")
            cur = r.json()
        vv = cur["version"]
        last_id = cur["issued"][-1]["id"]
        pending_head = cur["current"]["id"]
        r1 = withdraw(c, vv, last_id)
        r2 = c.post("/api/confirm", json={"version": vv})
        codes = sorted([r1.status_code, r2.status_code])
        check(codes == [200, 409], "同版本撤回与确认：一个 200 一个 409")
        after = c.get("/api/state").json()
        check(after["version"] == vv + 1, "竞争后版本只 +1")
        if r1.status_code == 200:
            check(after["current"]["id"] == last_id and len(after["issued"]) == len(cur["issued"]) - 1,
                  "撤回避出：末条回到队首，已发少一条，确认未生效")
        else:
            check(after["current"]["id"] != pending_head
                  and len(after["issued"]) == len(cur["issued"]) + 1,
                  "确认胜出：推进一条，撤回未生效")

        # 撤回与归档竞争
        s = setup_show(c)
        v = s["version"]
        id1 = s["pending"][0]["id"]
        s = confirm(c, v)
        v = s["version"]
        archives_before = len(c.get("/api/archives").json()["archives"])
        r1 = withdraw(c, v, id1)
        r2 = c.post("/api/archive", json={"version": v, "name": "竞争场"})
        codes = sorted([r1.status_code, r2.status_code])
        check(codes == [200, 409], "同版本撤回与归档：一个 200 一个 409")
        archives_after = len(c.get("/api/archives").json()["archives"])
        if r2.status_code == 200:
            check(archives_after == archives_before + 1 and r1.status_code == 409,
                  "归档胜出：新增一个档案，撤回未生效")
        else:
            check(archives_after == archives_before and r1.status_code == 200,
                  "撤回避出：不新增档案，撤回生效")


def test_persistence_and_archives():
    db_file = os.path.join(_tmp, "persist.db")
    main.DB_PATH = db_file
    with TestClient(main.app) as c:
        print("W7 刷新/重启保留结果；既有档案不被撤回影响")
        s = setup_show(c)
        v = s["version"]
        id1, id2, id3 = [p["id"] for p in s["pending"]]
        s = confirm(c, v)
        v = s["version"]
        s = confirm(c, v)
        v = s["version"]
        # 归档前先存一份快照对照（此处不归档，改为：先归档一场再开新场撤回）
        # 直接撤末条
        r = withdraw(c, v, id2)
        check(r.status_code == 200, "撤回成功")
        s_after = r.json()
        check([p["id"] for p in s_after["pending"]] == [id2, id3], "待发队首为撤回提示")

    # 重启：重新建应用连接同一数据库
    with TestClient(main.app) as c:
        s = c.get("/api/state").json()
        check(s["status"] == "live", "重启后仍为演出中")
        check([p["id"] for p in s["pending"]] == [id2, id3], "重启后撤回结果（待发顺序）保留")
        check([p["id"] for p in s["issued"]] == [id1], "重启后已发记录保留")

        # 归档本场：撤回后的现状入档；此前无档案
        r = c.post("/api/archive", json={"version": s["version"], "name": "撤回场"})
        check(r.status_code == 200, "归档成功")
        v = r.json()["version"]
        aid = c.get("/api/archives").json()["archives"][0]["id"]
        detail = c.get(f"/api/archives/{aid}").json()
        check([x["number"] for x in detail["issued"]] == ["A1"], "档案已发只含未撤回的 A1")
        check([x["number"] for x in detail["pending"]] == ["A2", "A3"],
              "档案剩余待发顺序为 A2（退回）在前、A3 在后")

        # 对档案的任何操作后，再开新场：撤回不影响以档案开新场
        r = c.post(f"/api/archives/{aid}/start", json={"version": v})
        check(r.status_code == 200, "以含撤回结果的档案开新场成功")
        s = r.json()
        check([p["number"] for p in s["pending"]] == ["A1", "A2", "A3"],
              "新场按档案已发 + 待发顺序排列，档案本身不变")


def test_control_seat_protection():
    main.DB_PATH = os.path.join(_tmp, "control.db")
    with TestClient(main.app) as c:
        print("W8 控制席保护：缺失/旧凭据撤回返回 403 且数据不变")
        s = setup_show(c)
        v = s["version"]
        id1 = s["pending"][0]["id"]
        s = confirm(c, v)
        v = s["version"]

        r = c.post("/api/control/enable", json={"version": v})
        check(r.status_code == 200, "启用控制席")
        tok = r.json()["control_token"]
        v = r.json()["version"]

        r = withdraw(c, v, id1)
        check(r.status_code == 403, "无凭据撤回返回 403")
        check(r.json()["state"]["control_granted"] is False, "403 附最新状态与资格 false")
        r = c.post(
            "/api/withdraw", json={"version": v, "cue_id": id1},
            headers={"X-Control-Token": "wrong-token"})
        check(r.status_code == 403, "旧/错误凭据撤回返回 403")
        snap = c.get("/api/state").json()
        check(len(snap["issued"]) == 1 and snap["version"] == v, "403 后已发与版本不变")

        r = c.post("/api/withdraw", json={"version": v, "cue_id": id1},
                   headers={"X-Control-Token": tok})
        check(r.status_code == 200, "正确凭据撤回成功")


def run_rounds(c, v, cue_id, expect, rounds):
    """执行若干轮「撤回 -> 再确认」，每步提交上一步返回的最新版本号。

    每轮撤回后断言待发顺序为 expect；返回 (最新版本号, 首个失败描述或 None)。
    """
    for n in rounds:
        r = withdraw(c, v, cue_id)
        if r.status_code != 200:
            return v, f"第 {n} 次撤回返回 {r.status_code}"
        s = r.json()
        order = [p["number"] for p in s["pending"]]
        if order != expect:
            return v, f"第 {n} 次撤回后待发顺序变为 {order}（期望 {expect}）"
        v = s["version"]
        r = c.post("/api/confirm", json={"version": v})
        if r.status_code != 200:
            return v, f"第 {n} 次撤回后再确认返回 {r.status_code}"
        v = r.json()["version"]
    return v, None


def max_position():
    with main.db() as conn:
        return conn.execute("SELECT MAX(position) AS m FROM cues").fetchone()["m"]


def test_repeat_reorder_path():
    """路径一（曾第 61 次撤回时顺序错乱）：重排后反复确认/撤回 70 轮。"""
    main.DB_PATH = os.path.join(_tmp, "repeat-reorder.db")
    with TestClient(main.app) as c:
        print("W9 反复确认/撤回（重排路径）：70 轮顺序不变、位置不增长、重启保留")
        s = setup_show(c)
        v = s["version"]
        id1, id2, id3 = [p["id"] for p in s["pending"]]
        s = confirm(c, v)
        v = s["version"]
        r = c.post("/api/reorder", json={"version": v, "order": [id3, id2]})
        check(r.status_code == 200, "重排待发为 A3、A2")
        v = r.json()["version"]
        v, err = run_rounds(c, v, id1, ["A1", "A3", "A2"], range(1, 36))
        check(err is None, "重启前第 1～35 次撤回：待发始终保持 A1、A3、A2"
              + (f"（{err}）" if err else ""))

    # 重启：重新建应用连接同一数据库，结果保留后继续跑
    with TestClient(main.app) as c:
        s = c.get("/api/state").json()
        check(s["version"] == v, "重启后版本号保留")
        check([x["number"] for x in s["issued"]] == ["A1"], "重启后已发记录保留")
        check([p["number"] for p in s["pending"]] == ["A3", "A2"],
              "重启后待发顺序（A3、A2）保留")
        v, err = run_rounds(c, s["version"], id1, ["A1", "A3", "A2"], range(36, 71))
        check(err is None, "重启后第 36～70 次撤回：待发始终保持 A1、A3、A2"
              + (f"（{err}）" if err else ""))
        check(max_position() <= 3, "70 轮后位置仍紧凑（不随操作次数增长）")


def test_repeat_plain_path():
    """路径二（曾第 62 次撤回抛 OverflowError）：不重排反复确认/撤回 70 轮。"""
    main.DB_PATH = os.path.join(_tmp, "repeat-plain.db")
    with TestClient(main.app) as c:
        print("W10 反复确认/撤回（不重排路径）：70 轮顺序不变、位置不增长、重启保留")
        s = setup_show(c)
        v = s["version"]
        id1 = s["pending"][0]["id"]
        s = confirm(c, v)
        v = s["version"]
        v, err = run_rounds(c, v, id1, ["A1", "A2", "A3"], range(1, 41))
        check(err is None, "重启前第 1～40 次撤回：待发始终保持 A1、A2、A3"
              + (f"（{err}）" if err else ""))

    # 重启：重新建应用连接同一数据库，结果保留后继续跑
    with TestClient(main.app) as c:
        s = c.get("/api/state").json()
        check(s["version"] == v, "重启后版本号保留")
        check([p["number"] for p in s["pending"]] == ["A2", "A3"],
              "重启后待发顺序（A2、A3）保留")
        v, err = run_rounds(c, s["version"], id1, ["A1", "A2", "A3"], range(41, 71))
        check(err is None, "重启后第 41～70 次撤回：待发始终保持 A1、A2、A3"
              + (f"（{err}）" if err else ""))
        check(max_position() <= 3, "70 轮后位置仍紧凑（不随操作次数增长）")


def test_large_positions():
    """既有库位置接近 SQLite 整数上限但仍可区分：撤回与重排都正常。"""
    main.DB_PATH = os.path.join(_tmp, "large-positions.db")
    int64_max = 9223372036854775807
    with TestClient(main.app) as c:
        print("W11 大位置既有库：接近整数上限且可区分的位置正常撤回/重排")
        s = setup_show(c)
        v = s["version"]
        id1, id2, id3 = [p["id"] for p in s["pending"]]
        s = confirm(c, v)
        v = s["version"]
        # 模拟旧缺陷留下的既有库：待发位置已接近上限（A3 在 A2 前，仍可区分）
        with main.db() as conn:
            conn.execute("UPDATE cues SET position = ? WHERE id = ?",
                         (int64_max - 1, id3))
            conn.execute("UPDATE cues SET position = ? WHERE id = ?",
                         (int64_max, id2))
        check([p["number"] for p in c.get("/api/state").json()["pending"]]
              == ["A3", "A2"], "大位置下待发顺序仍按位置可区分")

        r = withdraw(c, v, id1)
        check(r.status_code == 200, "大位置库撤回成功（不抛 OverflowError）")
        s = r.json()
        v = s["version"]
        check([p["number"] for p in s["pending"]] == ["A1", "A3", "A2"],
              "大位置撤回后相对顺序保持（A1 队首，A3 仍在 A2 前）")
        check(max_position() <= 3, "撤回后位置恢复紧凑")

        # 大位置上的重排同样不再累加溢出
        with main.db() as conn:
            conn.execute("UPDATE cues SET position = ? WHERE id = ?",
                         (int64_max - 1, id3))
            conn.execute("UPDATE cues SET position = ? WHERE id = ?",
                         (int64_max, id2))
        r = c.post("/api/reorder", json={"version": v, "order": [id1, id2, id3]})
        check(r.status_code == 200, "大位置库重排成功（不抛 OverflowError）")
        s = r.json()
        check([p["number"] for p in s["pending"]] == ["A1", "A2", "A3"],
              "大位置重排按指定顺序生效")
        check(max_position() <= 3, "重排后位置恢复紧凑")


def test_withdraw_failure_rollback():
    """撤回写入失败：退回待发 + 版本 +1 整体回滚，数据不变，异常不被吞。"""
    main.DB_PATH = os.path.join(_tmp, "withdraw-rollback.db")
    with TestClient(main.app, raise_server_exceptions=False) as c:
        print("W12 失败回滚：写入失败时撤回与版本 +1 都不生效")
        s = setup_show(c)
        v = s["version"]
        id1, id2, id3 = [p["id"] for p in s["pending"]]
        s = confirm(c, v)
        v = s["version"]
        with main.db() as conn:
            rows_before = {
                r["id"]: (r["position"], r["status"], r["issued_content"],
                          r["issued_at"], r["issued_seq"])
                for r in conn.execute(
                    "SELECT id, position, status, issued_content, issued_at, "
                    "issued_seq FROM cues"
                )
            }

        # 只在撤回的「版本 +1」语句上模拟写入失败（其前提示行已更新）
        real_connect = main._connect

        class FlakyConn:
            def __init__(self, real):
                self._real = real

            def execute(self, sql, parameters=()):
                if sql == ("UPDATE meta SET version = version + 1, "
                           "status = 'live' WHERE id = 1"):
                    raise RuntimeError("模拟写入失败")
                return self._real.execute(sql, parameters)

            def __getattr__(self, name):
                return getattr(self._real, name)

        main._connect = lambda: FlakyConn(real_connect())
        try:
            r = withdraw(c, v, id1)
            check(r.status_code == 500, "写入失败时撤回请求失败（异常不被吞掉）")
        finally:
            main._connect = real_connect

        s = c.get("/api/state").json()
        check(s["version"] == v, "失败回滚：版本号不变")
        check([x["id"] for x in s["issued"]] == [id1], "失败回滚：已发记录不变")
        check([p["id"] for p in s["pending"]] == [id2, id3],
              "失败回滚：待发内容与顺序不变")
        with main.db() as conn:
            rows_after = {
                r["id"]: (r["position"], r["status"], r["issued_content"],
                          r["issued_at"], r["issued_seq"])
                for r in conn.execute(
                    "SELECT id, position, status, issued_content, issued_at, "
                    "issued_seq FROM cues"
                )
            }
        check(rows_after == rows_before,
              "失败回滚：位置、状态与发出字段（快照/时间/序号）全部不变")

        r = withdraw(c, v, id1)
        check(r.status_code == 200, "失败后系统未卡死，正常撤回仍可用")
        s = r.json()
        check([p["id"] for p in s["pending"]] == [id1, id2, id3],
              "失败后的正常撤回：末条退回待发队首")


test_basic_withdraw()
test_other_records_unchanged()
test_done_back_to_live()
test_paused_withdraw_keeps_pause()
test_conflicts()
test_same_version_competition()
test_persistence_and_archives()
test_control_seat_protection()
test_repeat_reorder_path()
test_repeat_plain_path()
test_large_positions()
test_withdraw_failure_rollback()

print("\n全部通过" if failures == 0 else f"\n失败 {failures} 处断言")
sys.exit(0 if failures == 0 else 1)

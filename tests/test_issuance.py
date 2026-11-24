# -*- coding: utf-8 -*-
"""签发 / 作废 / 召回端到端测试。

覆盖需求：
- 换交可填孔内唯一化验单号，改段与手动重算沿用该号；
- 为生效底表填报告号签发，只签带化验单号的版本；底表换版拒签提示刷新；
  每孔每个报告号只签一次；
- 按报告号查召回状态并下载签发当时 CSV；同一版多报告只留一份快照，改段后可查档；
- 按化验单号 + 该单样号登记作废，只召回签发时供数行实际引用该化验的报告，
  列出报告号与深度段（走索引表，不逐份读底表）；
- 作废先保存则拒签仍引用该化验的底表，签发先保存则作废时召回；
- 保存失败时本次签发/作废整体不生效（单事务回滚）。
"""
import csv
import io

import pytest

from app import create_app
from app.services import ServiceError, issue_supply, register_void, recall_query


CSV_HEADER = "样号,起厘米,止厘米,品位ppm,复验标记\n"


@pytest.fixture()
def app(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "test.db")})
    yield app


@pytest.fixture()
def client(app):
    return app.test_client()


def _make_hole(app, client, code="ZK001"):
    client.post("/holes", data={"code": code, "name": "一号孔"}, follow_redirects=True)
    with app.app_context():
        from app.db import get_db
        return get_db().execute(
            "SELECT id FROM hole WHERE code = ?", (code,)
        ).fetchone()["id"]


def _set_segments(client, hole_id, text):
    return client.post(
        f"/holes/{hole_id}/segments",
        data={"segments_text": text},
        follow_redirects=True,
    )


def _upload(client, hole_id, content, order_no="HX-1"):
    data = {
        "assay_csv": (io.BytesIO(content.encode("utf-8")), "assay.csv"),
        "assay_order_no": order_no or "",
    }
    return client.post(
        f"/holes/{hole_id}/upload", data=data,
        content_type="multipart/form-data", follow_redirects=True,
    )


def _seed_two_ok_segments(app, client, hole_id, order_no="HX-1"):
    """段1 [0,100)：S1+S2 供数；段2 [100,200)：S3 供数；段3 [200,300)：S4 铺不满。"""
    _set_segments(client, hole_id, "0 100\n100 200\n200 300")
    text = CSV_HEADER + (
        "S1,0,50,100,否\n"
        "S2,50,100,200,否\n"
        "S3,100,200,300,是\n"
        "S4,200,260,500,否\n"  # 段3 待补样：S4 不进任何供数行
    )
    resp = _upload(client, hole_id, text, order_no=order_no)
    assert "换交成功" in resp.get_data(as_text=True)
    with app.app_context():
        from app.db import get_db
        return get_db().execute(
            "SELECT effective_version_id FROM hole WHERE id = ?", (hole_id,)
        ).fetchone()["effective_version_id"]


class TestAssayOrderNo:
    def test_order_no_stored_and_unique_within_hole(self, app, client):
        hole_id = _make_hole(app, client)
        _seed_two_ok_segments(app, client, hole_id, order_no="HX-1")
        # 同孔重复单号被拒
        resp = _upload(
            client, hole_id, CSV_HEADER + "S1,0,100,300,否\n", order_no="HX-1"
        )
        assert "化验单号 HX-1 在该孔已存在" in resp.get_data(as_text=True)
        # 不同单号可换交
        resp = _upload(
            client, hole_id, CSV_HEADER + "S1,0,100,300,否\n", order_no="HX-2"
        )
        assert "换交成功" in resp.get_data(as_text=True)
        with app.app_context():
            from app.db import get_db
            rows = get_db().execute(
                "SELECT seq, assay_order_no FROM assay_version "
                "WHERE hole_id = ? ORDER BY seq", (hole_id,)
            ).fetchall()
            assert [r["assay_order_no"] for r in rows][-1] == "HX-2"

    def test_recompute_and_segment_change_inherit_order_no(self, app, client):
        hole_id = _make_hole(app, client)
        _seed_two_ok_segments(app, client, hole_id, order_no="HX-9")
        # 改段自动重算，沿用单号
        _set_segments(client, hole_id, "0 100\n100 200")
        # 手动重算，仍沿用单号
        client.post(f"/holes/{hole_id}/recompute", follow_redirects=True)
        with app.app_context():
            from app.db import get_db
            rows = get_db().execute(
                "SELECT DISTINCT assay_order_no FROM assay_version WHERE hole_id = ?",
                (hole_id,),
            ).fetchall()
            assert [r["assay_order_no"] for r in rows] == ["HX-9"]


class TestIssue:
    def test_issue_requires_order_no(self, app, client):
        hole_id = _make_hole(app, client)
        _set_segments(client, hole_id, "0 100")
        _upload(client, hole_id, CSV_HEADER + "S1,0,100,300,否\n", order_no="")
        with app.app_context():
            with pytest.raises(ServiceError, match="没有化验单号"):
                issue_supply(hole_id, "R-1")

    def test_issue_happy_path_and_duplicate_report_rejected(self, app, client):
        hole_id = _make_hole(app, client)
        vid = _seed_two_ok_segments(app, client, hole_id)
        with app.app_context():
            issue_supply(hole_id, "R-1", expected_version_id=vid)
            with pytest.raises(ServiceError, match="只签一次"):
                issue_supply(hole_id, "R-1", expected_version_id=vid)
            from app.db import get_db
            # 引用只登记供数段（ok）样号：S1/S2/S3，不含待补样的 S4
            refs = get_db().execute(
                "SELECT DISTINCT sample_no FROM issuance_sample_ref "
                "WHERE hole_id = ? ORDER BY sample_no", (hole_id,)
            ).fetchall()
            assert [r["sample_no"] for r in refs] == ["S1", "S2", "S3"]

    def test_stale_version_rejected_with_refresh_hint(self, app, client):
        hole_id = _make_hole(app, client)
        old_vid = _seed_two_ok_segments(app, client, hole_id, order_no="HX-1")
        # 底表换版
        _upload(
            client, hole_id,
            CSV_HEADER + "S1,0,100,330,否\n",
            order_no="HX-2",
        )
        resp = client.post(
            f"/holes/{hole_id}/issue",
            data={"report_no": "R-9", "expected_version_id": old_vid},
            follow_redirects=True,
        )
        assert "底表已换版，请刷新" in resp.get_data(as_text=True)
        with app.app_context():
            from app.db import get_db
            assert get_db().execute(
                "SELECT COUNT(*) c FROM issuance WHERE hole_id = ?", (hole_id,)
            ).fetchone()["c"] == 0

    def test_same_version_multiple_reports_share_one_snapshot(self, app, client):
        hole_id = _make_hole(app, client)
        vid = _seed_two_ok_segments(app, client, hole_id)
        with app.app_context():
            issue_supply(hole_id, "R-A", expected_version_id=vid)
            issue_supply(hole_id, "R-B", expected_version_id=vid)
            from app.db import get_db
            n_snap = get_db().execute(
                "SELECT COUNT(*) c FROM supply_snapshot WHERE version_id = ?", (vid,)
            ).fetchone()["c"]
            assert n_snap == 1
            n_iss = get_db().execute(
                "SELECT COUNT(*) c FROM issuance WHERE hole_id = ?", (hole_id,)
            ).fetchone()["c"]
            assert n_iss == 2

    def test_snapshot_equals_live_supply_csv(self, app, client):
        hole_id = _make_hole(app, client)
        vid = _seed_two_ok_segments(app, client, hole_id)
        with app.app_context():
            issue_supply(hole_id, "R-CSV", expected_version_id=vid)
            from app.db import get_db
            snap = get_db().execute(
                "SELECT csv_content FROM supply_snapshot WHERE version_id = ?", (vid,)
            ).fetchone()["csv_content"]
        live = client.get(f"/holes/{hole_id}/supply.csv").data.decode("utf-8-sig")
        assert snap == live


class TestVoidAndRecall:
    def _two_reports_issued(self, app, client):
        hole_id = _make_hole(app, client)
        vid = _seed_two_ok_segments(app, client, hole_id)
        with app.app_context():
            issue_supply(hole_id, "R-1", expected_version_id=vid)
            issue_supply(hole_id, "R-2", expected_version_id=vid)
        return hole_id, vid

    def test_void_unknown_sample_rejected(self, app, client):
        hole_id, _ = self._two_reports_issued(app, client)
        with app.app_context():
            with pytest.raises(ServiceError, match="没有化验单号"):
                register_void(hole_id, "HX-1", "NO-SUCH")
            with pytest.raises(ServiceError):
                register_void(hole_id, "HX-UNKNOWN", "S1")

    def test_duplicate_void_rejected(self, app, client):
        hole_id, _ = self._two_reports_issued(app, client)
        with app.app_context():
            register_void(hole_id, "HX-1", "S2", note="第一次")
            with pytest.raises(ServiceError, match="已登记作废"):
                register_void(hole_id, "HX-1", "S2")

    def test_issue_first_then_void_recalls_only_referencing_reports(self, app, client):
        hole_id, _ = self._two_reports_issued(app, client)
        with app.app_context():
            info = register_void(hole_id, "HX-1", "S2")
            # 两份报告签发时都引用了 S2（段1 供数行）
            assert sorted(info["recalled_reports"]) == ["R-1", "R-2"]
            # 报告号与对应深度段一并返回，全部来自引用索引表
            assert info["report_segments"] == {
                "R-1": [{"seq": 1, "start_cm": 0, "end_cm": 100}],
                "R-2": [{"seq": 1, "start_cm": 0, "end_cm": 100}],
            }
            # 召回查询：状态、命中样号、深度段仅段1
            q = recall_query(hole_id, "R-1")
            assert q["recalled"] is True
            assert [m["sample_no"] for m in q["matched"]] == ["S2"]
            assert q["segments"] == [{"seq": 1, "start_cm": 0, "end_cm": 100}]

    def test_unreferenced_sample_void_does_not_recall(self, app, client):
        # S4 只在待补样段出现，未进任何供数行：作废它不应召回报告
        hole_id, _ = self._two_reports_issued(app, client)
        with app.app_context():
            info = register_void(hole_id, "HX-1", "S4")
            assert info["recalled_reports"] == []
            q = recall_query(hole_id, "R-1")
            assert q["recalled"] is False and q["matched"] == []

    def test_void_first_blocks_new_issuance(self, app, client):
        hole_id, vid = self._two_reports_issued(app, client)
        with app.app_context():
            register_void(hole_id, "HX-1", "S3")  # 段2 供数样号
            # 旧报告已召回
            assert recall_query(hole_id, "R-2")["recalled"] is True
            # 仍引用 S3 的当前底表拒签
            with pytest.raises(ServiceError, match="已作废化验样号 S3"):
                issue_supply(hole_id, "R-3", expected_version_id=vid)

    def test_recall_query_unknown_report(self, app, client):
        hole_id, _ = self._two_reports_issued(app, client)
        with app.app_context():
            assert recall_query(hole_id, "GHOST") is None

    def test_snapshot_remains_after_segment_change(self, app, client):
        hole_id, vid = self._two_reports_issued(app, client)
        with app.app_context():
            from app.db import get_db
            from app.services import build_version_supply_csv
            archived = build_version_supply_csv(get_db(), vid)
        # 改段触发重算（沿用单号），版本推进
        _set_segments(client, hole_id, "0 150\n150 200")
        dl = client.get(f"/holes/{hole_id}/recall/R-1/snapshot.csv")
        assert dl.status_code == 200
        assert dl.data.decode("utf-8-sig") == archived
        # 页面查询入口仍可查到召回状态
        resp = client.get(f"/holes/{hole_id}/recall?report_no=R-1",
                          follow_redirects=True)
        assert "报告召回状态" in resp.get_data(as_text=True)


class TestAtomicity:
    def test_failed_issue_rolls_back_everything(self, app, client):
        hole_id = _make_hole(app, client)
        _set_segments(client, hole_id, "0 100")
        _upload(
            client, hole_id,
            CSV_HEADER + "BOOM,0,100,300,否\n",
            order_no="HX-T",
        )
        with app.app_context():
            from app.db import get_db
            db = get_db()
            db.execute(
                "CREATE TRIGGER boom_iss BEFORE INSERT ON issuance_sample_ref "
                "WHEN NEW.sample_no = 'BOOM' "
                "BEGIN SELECT RAISE(ABORT, 'simulated save failure'); END"
            )
            db.commit()
            with pytest.raises(Exception):
                issue_supply(hole_id, "R-FAIL")
            # 签发、快照、引用全部回滚，没有残留
            assert db.execute("SELECT COUNT(*) c FROM issuance").fetchone()["c"] == 0
            assert db.execute("SELECT COUNT(*) c FROM supply_snapshot").fetchone()["c"] == 0
            assert db.execute("SELECT COUNT(*) c FROM issuance_sample_ref").fetchone()["c"] == 0

    def test_failed_void_rolls_back_recall(self, app, client):
        hole_id = _make_hole(app, client)
        vid = _seed_two_ok_segments(app, client, hole_id, order_no="HX-T")
        with app.app_context():
            from app.db import get_db
            issue_supply(hole_id, "R-KEEP", expected_version_id=vid)
            db = get_db()
            db.execute(
                "CREATE TRIGGER boom_void BEFORE INSERT ON assay_void "
                "WHEN NEW.note = 'BOOM' "
                "BEGIN SELECT RAISE(ABORT, 'simulated save failure'); END"
            )
            db.commit()
            with pytest.raises(Exception):
                register_void(hole_id, "HX-T", "S1", note="BOOM")
            assert db.execute("SELECT COUNT(*) c FROM assay_void").fetchone()["c"] == 0
            # 报告未被召回
            assert db.execute(
                "SELECT recalled FROM issuance WHERE report_no = 'R-KEEP'"
            ).fetchone()["recalled"] == 0


class TestHttpRoutes:
    def test_full_flow_via_http(self, app, client):
        hole_id = _make_hole(app, client)
        _seed_two_ok_segments(app, client, hole_id)
        with app.app_context():
            from app.db import get_db
            vid = get_db().execute(
                "SELECT effective_version_id FROM hole WHERE id = ?", (hole_id,)
            ).fetchone()["effective_version_id"]

        # 页面包含签发/作废/召回入口
        page = client.get(f"/holes/{hole_id}").get_data(as_text=True)
        assert "/issue" in page and "/void" in page and "/recall" in page
        assert "HX-1" in page

        # 签发
        resp = client.post(
            f"/holes/{hole_id}/issue",
            data={"report_no": "R-WEB", "expected_version_id": vid},
            follow_redirects=True,
        )
        assert "已签发" in resp.get_data(as_text=True)

        # 作废 -> 召回（flash 同时列出报告号与对应深度段）
        resp = client.post(
            f"/holes/{hole_id}/void",
            data={"assay_order_no": "HX-1", "sample_no": "S2", "note": "实验室通知"},
            follow_redirects=True,
        )
        body = resp.get_data(as_text=True)
        assert "召回报告：R-WEB" in body
        assert "[0,100)" in body.replace(" ", "")

        # 召回查询页 + 快照下载
        q = client.get(f"/holes/{hole_id}/recall?report_no=R-WEB")
        body = q.get_data(as_text=True)
        assert "已召回" in body and "第 1 段 [0, 100)" in body
        dl = client.get(f"/holes/{hole_id}/recall/R-WEB/snapshot.csv")
        assert dl.status_code == 200
        rows = list(csv.reader(io.StringIO(dl.data.decode("utf-8-sig"))))
        assert rows[0][:5] == ["孔号", "段序号", "起厘米", "止厘米", "状态"]

        # 未知报告号查询给提示而非报错
        resp = client.get(
            f"/holes/{hole_id}/recall?report_no=NOPE", follow_redirects=True
        )
        assert "未找到报告号" in resp.get_data(as_text=True)

    def test_invalid_expected_version_param_rejected(self, app, client):
        hole_id = _make_hole(app, client)
        _seed_two_ok_segments(app, client, hole_id)
        resp = client.post(
            f"/holes/{hole_id}/issue",
            data={"report_no": "R-BAD", "expected_version_id": "not-a-number"},
            follow_redirects=True,
        )
        assert "版本号参数无效" in resp.get_data(as_text=True)
        with app.app_context():
            from app.db import get_db
            assert get_db().execute(
                "SELECT COUNT(*) c FROM issuance WHERE report_no = 'R-BAD'"
            ).fetchone()["c"] == 0

    def test_recall_lookup_uses_indexed_ref_table(self, app, client):
        # 召回判定走 issuance_sample_ref 的 (hole_id, order_no, sample_no) 索引，
        # 不依赖解析任何快照 CSV。
        hid = _make_hole(app, client, code="ZKIDX")
        vid = _seed_two_ok_segments(app, client, hid)
        with app.app_context():
            from app.db import get_db
            issue_supply(hid, "R-IDX", expected_version_id=vid)
            indexes = [
                r["name"] for r in get_db().execute(
                    "PRAGMA index_list(issuance_sample_ref)"
                ).fetchall()
            ]
            assert "idx_ref_lookup" in indexes
            register_void(hid, "HX-1", "S3")
            q = recall_query(hid, "R-IDX")
            assert q["recalled"] is True
            assert q["segments"] == [{"seq": 2, "start_cm": 100, "end_cm": 200}]

# -*- coding: utf-8 -*-
"""业务服务：孔与统计段维护、按样号换交的原子版本化重算。

换交语义：一次上传的 CSV 代表该孔当前全套化验。以样号为主键与上一生效
版本比对，得到新增/消失/变更样号；化验行、逐段核查结果、底表结果在同一
事务内写入新版本，全部成功后才把 hole.effective_version_id 指向新版本，
旧版本保持只读快照，任何一步失败则整体回滚，旧底表照常可下载。
"""
from __future__ import annotations

import sqlite3
from typing import List, Optional, Sequence, Tuple

from .csvio import CsvError, export_supply_csv
from .db import get_db
from .tiling import Assay, SegmentVerdict, dumps_samples, evaluate_all, loads_samples


class ServiceError(ValueError):
    pass


# ---------------------------------------------------------------- 孔与统计段

def create_hole(code: str, name: str = "") -> int:
    conn = get_db()
    code = (code or "").strip()
    if not code:
        raise ServiceError("孔号不能为空")
    try:
        cur = conn.execute(
            "INSERT INTO hole(code, name) VALUES (?, ?)", (code, name.strip())
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        raise ServiceError(f"孔号 {code} 已存在")


def list_holes() -> List[sqlite3.Row]:
    return get_db().execute(
        """
        SELECT h.*, (
            SELECT COUNT(*) FROM segment_def s WHERE s.hole_id = h.id
        ) AS segment_count, (
            SELECT COUNT(*) FROM assay_row r
            JOIN assay_version v ON v.id = r.version_id
            WHERE v.id = h.effective_version_id
        ) AS assay_count
        FROM hole h ORDER BY h.code
        """
    ).fetchall()


def get_hole(hole_id: int) -> Optional[sqlite3.Row]:
    return get_db().execute("SELECT * FROM hole WHERE id = ?", (hole_id,)).fetchone()


def list_segments(hole_id: int) -> List[sqlite3.Row]:
    return get_db().execute(
        "SELECT * FROM segment_def WHERE hole_id = ? ORDER BY seq", (hole_id,)
    ).fetchall()


def replace_segments(hole_id: int, segments: Sequence[Tuple[int, int]]) -> None:
    """整体替换统计段定义：(start_cm, end_cm) 列表，自动编号并校验。"""
    hole = get_hole(hole_id)
    if hole is None:
        raise ServiceError("钻孔不存在")
    cleaned: List[Tuple[int, int]] = []
    for i, (s, e) in enumerate(segments, start=1):
        if not isinstance(s, int) or not isinstance(e, int):
            raise ServiceError(f"第 {i} 段：厘米值必须是整数")
        if e <= s:
            raise ServiceError(f"第 {i} 段：止厘米必须大于起厘米")
        cleaned.append((s, e))
    # 段间不允许重叠（底表深度段应互斥；相邻相接允许）。
    ordered = sorted(cleaned)
    for (s1, e1), (s2, e2) in zip(ordered, ordered[1:]):
        if s2 < e1:
            raise ServiceError(f"统计段重叠：[{s1},{e1}) 与 [{s2},{e2})")

    conn = get_db()
    try:
        with conn:  # 事务
            conn.execute("DELETE FROM segment_def WHERE hole_id = ?", (hole_id,))
            conn.executemany(
                "INSERT INTO segment_def(hole_id, seq, start_cm, end_cm) VALUES (?,?,?,?)",
                [(hole_id, i, s, e) for i, (s, e) in enumerate(ordered, start=1)],
            )
    except sqlite3.DatabaseError as exc:  # pragma: no cover - 兜底
        raise ServiceError(f"统计段保存失败：{exc}")

    # 段定义变了，基于当前生效化验重算一版（若无化验则跳过）。
    rows = latest_assays(hole_id)
    if rows:
        recompute(hole_id, change_note="统计段调整后重算")


# ---------------------------------------------------------------- 版本与化验

def latest_version(hole_id: int) -> Optional[sqlite3.Row]:
    hole = get_hole(hole_id)
    if hole is None or hole["effective_version_id"] is None:
        return None
    return get_db().execute(
        "SELECT * FROM assay_version WHERE id = ?", (hole["effective_version_id"],)
    ).fetchone()


def list_versions(hole_id: int) -> List[sqlite3.Row]:
    return get_db().execute(
        "SELECT * FROM assay_version WHERE hole_id = ? ORDER BY seq DESC", (hole_id,)
    ).fetchall()


def latest_assays(hole_id: int) -> List[Assay]:
    hole = get_hole(hole_id)
    if hole is None or hole["effective_version_id"] is None:
        return []
    rows = get_db().execute(
        "SELECT * FROM assay_row WHERE version_id = ? ORDER BY start_cm, sample_no",
        (hole["effective_version_id"],),
    ).fetchall()
    return [
        Assay(r["sample_no"], r["start_cm"], r["end_cm"], r["grade_ppm"], bool(r["recheck"]))
        for r in rows
    ]


def list_results(version_id: int) -> List[sqlite3.Row]:
    return get_db().execute(
        "SELECT * FROM segment_result WHERE version_id = ? ORDER BY seq", (version_id,)
    ).fetchall()


def _next_version_seq(conn: sqlite3.Connection, hole_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM assay_version WHERE hole_id = ?",
        (hole_id,),
    ).fetchone()
    return row["next_seq"]


def _diff_by_sample(old: Sequence[Assay], new: Sequence[Assay]) -> Tuple[int, int, int]:
    """按样号比对两套化验：返回 (新增, 消失, 变更) 计数。"""
    old_map = {a.sample_no: a for a in old}
    new_map = {a.sample_no: a for a in new}
    added = [k for k in new_map if k not in old_map]
    removed = [k for k in old_map if k not in new_map]
    changed = [
        k for k in (old_map.keys() & new_map.keys())
        if (
            old_map[k].start_cm != new_map[k].start_cm
            or old_map[k].end_cm != new_map[k].end_cm
            or old_map[k].grade_ppm != new_map[k].grade_ppm
            or old_map[k].recheck != new_map[k].recheck
        )
    ]
    return len(added), len(removed), len(changed)


def _persist_version(
    conn: sqlite3.Connection,
    hole_id: int,
    assays: Sequence[Assay],
    verdicts: Sequence[SegmentVerdict],
    change_note: str,
    diff: Tuple[int, int, int],
    assay_order_no: Optional[str] = None,
) -> int:
    """在调用方事务内写入版本快照并翻转生效指针，返回新版本 id。"""
    seq = _next_version_seq(conn, hole_id)
    cur = conn.execute(
        """
        INSERT INTO assay_version(hole_id, seq, status, assay_order_no, change_note,
                                  added_count, removed_count, changed_count)
        VALUES (?, ?, 'effective', ?, ?, ?, ?, ?)
        """,
        (hole_id, seq, assay_order_no, change_note, diff[0], diff[1], diff[2]),
    )
    version_id = cur.lastrowid
    conn.executemany(
        """
        INSERT INTO assay_row(version_id, sample_no, start_cm, end_cm, grade_ppm, recheck)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [(version_id, a.sample_no, a.start_cm, a.end_cm, a.grade_ppm, 1 if a.recheck else 0)
         for a in assays],
    )
    seg_rows = conn.execute(
        "SELECT id, seq, start_cm, end_cm FROM segment_def WHERE hole_id = ? ORDER BY seq",
        (hole_id,),
    ).fetchall()
    seg_by_seq = {r["seq"]: r for r in seg_rows}
    for v in verdicts:
        sd = seg_by_seq.get(v.seq)
        if sd is None:
            continue
        conn.execute(
            """
            INSERT INTO segment_result(
                version_id, segment_def_id, seq, start_cm, end_cm, status,
                grade_num, grade_den, recheck_cover_cm, covered_cm,
                sample_path_min, sample_path_max, grade_num_min, grade_num_max)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                version_id, sd["id"], v.seq, v.start_cm, v.end_cm, v.status,
                v.grade_num, v.grade_den, v.recheck_cover_cm, v.covered_cm,
                dumps_samples(v.samples_min), dumps_samples(v.samples_max),
                v.grade_num_min, v.grade_num_max,
            ),
        )
    # 旧生效版本降级；翻转指针——同一事务内原子完成。
    conn.execute(
        "UPDATE assay_version SET status = 'superseded' "
        "WHERE hole_id = ? AND id <> ? AND status = 'effective'",
        (hole_id, version_id),
    )
    conn.execute(
        "UPDATE hole SET effective_version_id = ? WHERE id = ?",
        (version_id, hole_id),
    )
    return version_id


def recompute(hole_id: int, change_note: str = "手动重算") -> int:
    """基于当前生效化验 + 当前统计段定义，生成新版本。无化验时拒绝。

    改段重算/手动重算沿用当前生效版本的化验单号。
    """
    conn = get_db()
    hole = conn.execute("SELECT * FROM hole WHERE id = ?", (hole_id,)).fetchone()
    if hole is None:
        raise ServiceError("钻孔不存在")
    current = latest_version(hole_id)
    if current is None:
        raise ServiceError("尚无化验数据，无法重算；请先上传化验 CSV")
    assay_order_no = current["assay_order_no"]
    assays = latest_assays(hole_id)
    if not assays:
        raise ServiceError("尚无化验数据，无法重算；请先上传化验 CSV")
    segments = [
        (r["seq"], r["start_cm"], r["end_cm"]) for r in list_segments(hole_id)
    ]
    if not segments:
        raise ServiceError("尚未定义统计段")

    verdicts = evaluate_all(segments, assays)
    diff = (0, 0, 0)
    with conn:  # 单事务：失败整体回滚，旧版本不受影响
        return _persist_version(
            conn, hole_id, assays, verdicts, change_note, diff,
            assay_order_no=assay_order_no,
        )


def upload_assays(
    hole_id: int,
    assays: Sequence[Assay],
    assay_order_no: Optional[str] = None,
    change_note: str = "按样号换交化验",
) -> dict:
    """换交入口：校验 -> 比对样号 -> 核算 -> 单事务落库并翻转版本。

    assay_order_no 为孔内唯一的化验单号（可空）；改段/重算版本由 recompute 沿用。
    """
    conn = get_db()
    hole = conn.execute("SELECT * FROM hole WHERE id = ?", (hole_id,)).fetchone()
    if hole is None:
        raise ServiceError("钻孔不存在")
    segments = list_segments(hole_id)
    if not segments:
        raise ServiceError("请先为该孔定义统计段，再上传化验 CSV")

    assay_order_no = (assay_order_no or "").strip() or None
    if assay_order_no is not None:
        dup = conn.execute(
            "SELECT 1 FROM assay_version WHERE hole_id = ? AND assay_order_no = ? LIMIT 1",
            (hole_id, assay_order_no),
        ).fetchone()
        if dup is not None:
            raise ServiceError(f"化验单号 {assay_order_no} 在该孔已存在，必须孔内唯一")

    # 样号唯一性已在 CSV 解析阶段保证；这里再防一层同孔内的硬约束。
    sample_nos = [a.sample_no for a in assays]
    if len(set(sample_nos)) != len(sample_nos):
        raise CsvError("样号存在重复")

    old_assays = latest_assays(hole_id)
    diff = _diff_by_sample(old_assays, assays)
    seg_tuples = [(r["seq"], r["start_cm"], r["end_cm"]) for r in segments]
    verdicts = evaluate_all(seg_tuples, assays)

    with conn:  # 单事务：化验、核查结果、底表一同生效
        version_id = _persist_version(
            conn, hole_id, assays, verdicts, change_note, diff,
            assay_order_no=assay_order_no,
        )

    counts = {
        "version_id": version_id,
        "added": diff[0],
        "removed": diff[1],
        "changed": diff[2],
        "ok": sum(1 for v in verdicts if v.status == "ok"),
        "diverged": sum(1 for v in verdicts if v.status == "diverged"),
        "incomplete": sum(1 for v in verdicts if v.status == "incomplete"),
    }
    return counts


# ---------------------------------------------------------------- 签发 / 召回 / 作废

def _verdict_view(row: sqlite3.Row):
    """把 segment_result 行适配为 export_supply_csv 需要的字段视图。"""

    class _V:
        def __init__(self, r):
            self.seq = r["seq"]
            self.start_cm = r["start_cm"]
            self.end_cm = r["end_cm"]
            self.status = r["status"]
            self.grade_num = r["grade_num"]
            self.grade_den = r["grade_den"]
            self.grade_num_min = r["grade_num_min"]
            self.grade_num_max = r["grade_num_max"]
            self.recheck_cover_cm = r["recheck_cover_cm"]
            self.samples_min = loads_samples(r["sample_path_min"])
            self.samples_max = loads_samples(r["sample_path_max"])

        @property
        def length(self):
            return self.end_cm - self.start_cm

    return _V(row)


def build_version_supply_csv(conn: sqlite3.Connection, version_id: int) -> str:
    """按版本快照行还原供数底表 CSV（与下载页同一导出格式）。"""
    version = conn.execute(
        "SELECT v.*, h.code AS hole_code FROM assay_version v "
        "JOIN hole h ON h.id = v.hole_id WHERE v.id = ?",
        (version_id,),
    ).fetchone()
    if version is None:
        raise ServiceError("版本不存在")
    rows = conn.execute(
        "SELECT * FROM segment_result WHERE version_id = ? ORDER BY seq",
        (version_id,),
    ).fetchall()
    return export_supply_csv(version["hole_code"], [_verdict_view(r) for r in rows])


def _get_or_create_snapshot(conn: sqlite3.Connection, hole_id: int, version_id: int) -> int:
    """同一版本只保留一份底表快照；多份报告签同一版时复用。"""
    row = conn.execute(
        "SELECT id FROM supply_snapshot WHERE version_id = ?", (version_id,)
    ).fetchone()
    if row is not None:
        return row["id"]
    csv_content = build_version_supply_csv(conn, version_id)
    cur = conn.execute(
        "INSERT INTO supply_snapshot(version_id, hole_id, csv_content) VALUES (?, ?, ?)",
        (version_id, hole_id, csv_content),
    )
    return cur.lastrowid


def _supply_refs(conn: sqlite3.Connection, version_id: int):
    """签发当时供数行（ok 段）实际引用的样号引用列表。

    返回 (sample_no, segment_seq, start_cm, end_cm)；分歧/待补样段不供数，不产生引用。
    """
    rows = conn.execute(
        "SELECT seq, start_cm, end_cm, sample_path_min, status "
        "FROM segment_result WHERE version_id = ? AND status = 'ok' ORDER BY seq",
        (version_id,),
    ).fetchall()
    refs = []
    for r in rows:
        for sample_no in loads_samples(r["sample_path_min"]):
            refs.append((sample_no, r["seq"], r["start_cm"], r["end_cm"]))
    return refs


def issue_supply(hole_id: int, report_no: str, expected_version_id: Optional[int] = None) -> dict:
    """为页面所见的生效底表签发报告号。

    规则：
    - 报告号必填，每孔每个报告号只签一次；
    - 只签带化验单号的版本；
    - expected_version_id 与当前生效版本不一致（底表已换版）则拒签，提示刷新；
    - 作废先保存：仍引用已作废化验（单号+样号）的底表拒签；
    - 全程单事务，任何失败整体回滚，本次签发不生效。
    """
    conn = get_db()
    report_no = (report_no or "").strip()
    if not report_no:
        raise ServiceError("报告号不能为空")
    with conn:  # 单事务：保存失败时本次签发整体不生效
        hole = conn.execute("SELECT * FROM hole WHERE id = ?", (hole_id,)).fetchone()
        if hole is None:
            raise ServiceError("钻孔不存在")
        if hole["effective_version_id"] is None:
            raise ServiceError("尚无生效底表，无法签发")
        version_id = hole["effective_version_id"]
        if expected_version_id is not None and int(expected_version_id) != version_id:
            raise ServiceError("底表已换版，请刷新页面后按当前生效底表重新签发")
        version = conn.execute(
            "SELECT * FROM assay_version WHERE id = ?", (version_id,)
        ).fetchone()
        assay_order_no = version["assay_order_no"]
        if not assay_order_no:
            raise ServiceError("当前生效版本没有化验单号，不能签发；请重新换交并填写化验单号")
        dup = conn.execute(
            "SELECT 1 FROM issuance WHERE hole_id = ? AND report_no = ? LIMIT 1",
            (hole_id, report_no),
        ).fetchone()
        if dup is not None:
            raise ServiceError(f"报告号 {report_no} 在该孔已签发过，每孔每个报告号只签一次")

        refs = _supply_refs(conn, version_id)
        # 作废先保存就拒签：任一供数引用命中作废登记即拒绝。
        if refs:
            sample_nos = list({r[0] for r in refs})
            placeholders = ",".join("?" * len(sample_nos))
            hit = conn.execute(
                f"SELECT sample_no FROM assay_void "
                f"WHERE hole_id = ? AND assay_order_no = ? "
                f"AND sample_no IN ({placeholders}) LIMIT 1",
                [hole_id, assay_order_no, *sample_nos],
            ).fetchone()
            if hit is not None:
                raise ServiceError(
                    f"底表供数段仍引用已作废化验样号 {hit['sample_no']}（单号 {assay_order_no}），"
                    "请先换交去除该化验后再签发"
                )

        snapshot_id = _get_or_create_snapshot(conn, hole_id, version_id)
        cur = conn.execute(
            "INSERT INTO issuance(hole_id, version_id, report_no, snapshot_id) "
            "VALUES (?, ?, ?, ?)",
            (hole_id, version_id, report_no, snapshot_id),
        )
        issuance_id = cur.lastrowid
        conn.executemany(
            """
            INSERT INTO issuance_sample_ref(
                issuance_id, hole_id, assay_order_no, sample_no,
                segment_seq, start_cm, end_cm)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (issuance_id, hole_id, assay_order_no, sample_no, seq, s, e)
                for sample_no, seq, s, e in refs
            ],
        )
    return {"issuance_id": issuance_id, "version_id": version_id, "report_no": report_no}


def register_void(hole_id: int, assay_order_no: str, sample_no: str, note: str = "") -> dict:
    """按化验单号 + 该单样号登记作废，并原子召回受影响报告。

    - 单号/样号必须在该孔某版本化验中真实出现；
    - 同一 (孔, 单号, 样号) 不重复登记；
    - 只召回「签发时供数行实际引用了该化验」的报告（走 issuance_sample_ref 索引）；
    - 签发先保存就把该报告列入召回；作废与召回同事务，保存失败整体不生效。
    """
    conn = get_db()
    assay_order_no = (assay_order_no or "").strip()
    sample_no = (sample_no or "").strip()
    if not assay_order_no or not sample_no:
        raise ServiceError("化验单号和样号都不能为空")
    with conn:
        hole = conn.execute("SELECT id FROM hole WHERE id = ?", (hole_id,)).fetchone()
        if hole is None:
            raise ServiceError("钻孔不存在")
        exists = conn.execute(
            """
            SELECT 1 FROM assay_row r JOIN assay_version v ON v.id = r.version_id
            WHERE v.hole_id = ? AND v.assay_order_no = ? AND r.sample_no = ? LIMIT 1
            """,
            (hole_id, assay_order_no, sample_no),
        ).fetchone()
        if exists is None:
            raise ServiceError(
                f"该孔没有化验单号 {assay_order_no} 下的样号 {sample_no}，无法登记作废"
            )
        try:
            cur = conn.execute(
                "INSERT INTO assay_void(hole_id, assay_order_no, sample_no, note) "
                "VALUES (?, ?, ?, ?)",
                (hole_id, assay_order_no, sample_no, (note or "").strip()),
            )
        except sqlite3.IntegrityError:
            raise ServiceError(
                f"化验单号 {assay_order_no} 的样号 {sample_no} 已登记作废，不能重复登记"
            )
        void_id = cur.lastrowid
        # 只召回签发时实际引用了这条化验的报告；查询走索引，不读底表快照。
        recalled = conn.execute(
            """
            SELECT DISTINCT i.id AS issuance_id, i.report_no,
                   ref.segment_seq, ref.start_cm, ref.end_cm
            FROM issuance_sample_ref ref JOIN issuance i ON i.id = ref.issuance_id
            WHERE ref.hole_id = ? AND ref.assay_order_no = ? AND ref.sample_no = ?
              AND i.recalled = 0
            ORDER BY i.report_no, ref.segment_seq
            """,
            (hole_id, assay_order_no, sample_no),
        ).fetchall()
        report_segments: dict = {}
        for r in recalled:
            conn.execute("UPDATE issuance SET recalled = 1 WHERE id = ?", (r["issuance_id"],))
            report_segments.setdefault(r["report_no"], []).append({
                "seq": r["segment_seq"],
                "start_cm": r["start_cm"],
                "end_cm": r["end_cm"],
            })
    return {
        "void_id": void_id,
        "recalled_reports": sorted(report_segments),
        "report_segments": report_segments,
    }


def recall_query(hole_id: int, report_no: str) -> Optional[dict]:
    """按报告号查召回状态：无需逐份读取底表，全部走索引/关联表。"""
    conn = get_db()
    report_no = (report_no or "").strip()
    if not report_no:
        raise ServiceError("报告号不能为空")
    row = conn.execute(
        """
        SELECT i.*, v.assay_order_no, s.csv_content
        FROM issuance i
        JOIN assay_version v ON v.id = i.version_id
        JOIN supply_snapshot s ON s.id = i.snapshot_id
        WHERE i.hole_id = ? AND i.report_no = ?
        """,
        (hole_id, report_no),
    ).fetchone()
    if row is None:
        return None
    # 命中的作废化验及对应深度段（按段去重、保持段序）。
    matched = conn.execute(
        """
        SELECT DISTINCT ref.assay_order_no, ref.sample_no,
               ref.segment_seq, ref.start_cm, ref.end_cm,
               av.created_at AS voided_at
        FROM issuance_sample_ref ref
        JOIN assay_void av
          ON av.hole_id = ref.hole_id
         AND av.assay_order_no = ref.assay_order_no
         AND av.sample_no = ref.sample_no
        WHERE ref.issuance_id = ?
        ORDER BY ref.segment_seq, ref.sample_no
        """,
        (row["id"],),
    ).fetchall()
    segments = []
    seen_seg = set()
    for m in matched:
        key = (m["segment_seq"], m["start_cm"], m["end_cm"])
        if key not in seen_seg:
            seen_seg.add(key)
            segments.append({
                "seq": m["segment_seq"],
                "start_cm": m["start_cm"],
                "end_cm": m["end_cm"],
            })
    return {
        "report_no": row["report_no"],
        "version_id": row["version_id"],
        "assay_order_no": row["assay_order_no"],
        "recalled": bool(row["recalled"]),
        "created_at": row["created_at"],
        "csv_content": row["csv_content"],
        "matched": [dict(m) for m in matched],
        "segments": segments,
    }


def list_issuances(hole_id: int) -> List[sqlite3.Row]:
    return get_db().execute(
        """
        SELECT i.*, v.seq AS version_seq, v.assay_order_no
        FROM issuance i JOIN assay_version v ON v.id = i.version_id
        WHERE i.hole_id = ? ORDER BY i.id DESC
        """,
        (hole_id,),
    ).fetchall()


def list_voids(hole_id: int) -> List[sqlite3.Row]:
    return get_db().execute(
        "SELECT * FROM assay_void WHERE hole_id = ? ORDER BY id DESC", (hole_id,)
    ).fetchall()

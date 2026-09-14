"""``POST /api/v1/contracts`` 的正式集成测试（**需要真实 MySQL**）。

为什么放在 integration
---------------------
这里断言的全部是**数据库不变量**（唯一约束、外键、记录条数、字段取值），
以及真实文件落盘。SQLite 上跑不出同样的约束语义，结论没有意义。

⚠️ 为什么数据库断言用**同步 pymysql** 而不是 ORM
------------------------------------------------
``TestClient`` 会在自己的 portal 事件循环里跑 lifespan 并创建 AsyncEngine，
而测试函数本身是同步的。若此时用 ``asyncio.run()`` 查库，就是在**另一个事件循环**
里使用同一个 AsyncEngine —— 连接绑定在原来的循环上，会直接挂住。
用同步驱动读数与清理，既规避了这个陷阱，也让测试断言与业务代码的事件循环彻底解耦。

隔离方式
--------
存储根目录通过覆盖 ``app.storage.local`` 的模块级默认实例指向 ``tmp_path``，
因此不会往真实的 ``backend/storage/`` 写任何东西。
"""

from __future__ import annotations

import asyncio
import io
import uuid
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pymysql
import pytest
from fastapi.testclient import TestClient

import app.api.v1.endpoints.contracts as contracts_endpoint
import app.storage.local as storage_local
from app.core.config import get_settings
from app.main import app
from app.services import contract_ingest
from app.storage.local import LocalStorageBackend
from app.utils.hash_utils import build_idempotency_key

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PREFIX = "IT-API-"


def _connect() -> pymysql.connections.Connection:
    settings = get_settings()
    return pymysql.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.mysql_user,
        password=settings.mysql_password.get_secret_value(),
        database=settings.mysql_db,
        charset="utf8mb4",
        connect_timeout=5,
        autocommit=True,
    )


def _db_available() -> tuple[bool, str]:
    settings = get_settings()
    if not settings.mysql_password.get_secret_value():
        return False, ".env 中未配置 MYSQL_PASSWORD"
    try:
        _connect().close()
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}（MySQL 服务未启动或库不存在？）"
    else:
        return True, ""


_AVAILABLE, _REASON = _db_available()

pytestmark = pytest.mark.skipif(not _AVAILABLE, reason=f"MySQL 不可用：{_REASON}")


def _query(sql: str, params: tuple[Any, ...] = ()) -> list[tuple]:
    conn = _connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            return list(cursor.fetchall())
    finally:
        conn.close()


def _scalar(sql: str, params: tuple[Any, ...] = ()) -> Any:
    rows = _query(sql, params)
    return rows[0][0] if rows else None


def _purge() -> None:
    """清掉本模块产生的记录（顺序与外键依赖相反 —— FK 是 RESTRICT）。"""
    ids = [row[0] for row in _query("SELECT id FROM contract WHERE contract_no LIKE %s", (f"{PREFIX}%",))]
    if not ids:
        return
    placeholders = ",".join(["%s"] * len(ids))
    _query(f"DELETE FROM review_task WHERE contract_id IN ({placeholders})", tuple(ids))
    _query(f"DELETE FROM contract_file WHERE contract_id IN ({placeholders})", tuple(ids))
    _query(f"DELETE FROM contract WHERE id IN ({placeholders})", tuple(ids))


def _docx(token: str = "a") -> bytes:
    """生成内容可控的最小合法 DOCX（token 不同 ⇒ sha256 不同）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", f"<document>{token}</document>")
    return buf.getvalue()


def _unique_no() -> str:
    return f"{PREFIX}{uuid.uuid4().hex[:12]}"


def _form(contract_no: str, **overrides: str) -> dict[str, str]:
    data = {"contract_no": contract_no, "title": "集成测试合同", "contract_type": "PURCHASE"}
    data.update(overrides)
    return data


def _files(payload: bytes, filename: str = "合同.docx", mime: str = DOCX_MIME) -> dict:
    return {"file": (filename, payload, mime)}


@pytest.fixture(autouse=True)
def _isolated_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[LocalStorageBackend]:
    """把默认存储后端指向 tmp_path，避免污染 backend/storage/。"""
    backend = LocalStorageBackend(tmp_path / "storage")
    monkeypatch.setattr(storage_local, "_default_backend", backend)
    yield backend


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _cleanup() -> Iterator[None]:
    yield
    _purge()
    asyncio.run(_dispose())


async def _dispose() -> None:
    """关闭 TestClient 留下的引擎，避免连接泄漏到后续用例。"""
    from app.db.session import dispose_engine

    await dispose_engine()


# --------------------------------------------------------------------------- #
# 4. 正常上传与落库不变量
# --------------------------------------------------------------------------- #
def test_minimal_upload_creates_exactly_one_of_each(client: TestClient) -> None:
    contract_no = _unique_no()
    payload = _docx()

    response = client.post("/api/v1/contracts", files=_files(payload), data=_form(contract_no))

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["reused"] is False, "首次上传：文件层未复用"
    assert body["task_reused"] is False, "首次上传：任务层未复用"
    assert body["contract_no"] == contract_no
    assert body["file_type"] == "DOCX"

    contracts = _query(
        "SELECT id, current_task_id, status FROM contract WHERE contract_no = %s", (contract_no,)
    )
    assert len(contracts) == 1, "必须恰好一条 Contract"
    contract_id, current_task_id, contract_status = contracts[0]

    files = _query(
        "SELECT id, sha256, parse_status FROM contract_file WHERE contract_id = %s", (contract_id,)
    )
    assert len(files) == 1, "必须恰好一条 ContractFile"
    file_id, sha256, parse_status = files[0]

    tasks = _query(
        "SELECT id, file_id, status, current_stage, idempotency_key FROM review_task WHERE contract_id = %s",
        (contract_id,),
    )
    assert len(tasks) == 1, "必须恰好一条 ReviewTask"
    task_id, task_file_id, task_status, task_stage, idem_key = tasks[0]

    # ---- 字段级不变量 ----
    assert current_task_id == task_id, "current_task_id 必须指向本次任务"
    assert contract_status == "pending"
    assert sha256 == body["sha256"] and len(sha256) == 64
    assert parse_status == "PENDING"
    assert task_status == "pending"
    assert task_stage == "UPLOADED"
    assert task_file_id == file_id
    assert len(idem_key) == 64, "幂等键必须适配 VARCHAR(64)"


def test_uploaded_file_is_actually_stored_and_path_hides_original_filename(
    client: TestClient, _isolated_storage: LocalStorageBackend
) -> None:
    payload = _docx("store")
    response = client.post(
        "/api/v1/contracts",
        files=_files(payload, filename="机密采购合同-2026.docx"),
        data=_form(_unique_no()),
    )
    assert response.status_code == 201
    body = response.json()

    key = _isolated_storage.build_upload_key(
        contract_id=body["contract_id"], sha256=body["sha256"], extension=".docx"
    )
    stored = _isolated_storage.resolve(key)
    assert stored.is_file(), "文件必须真实落盘"
    assert stored.read_bytes() == payload, "落盘内容必须与上传一致"

    # 用户原始文件名不得出现在存储路径里（只作为 metadata 存在 file_name）
    assert "机密采购合同" not in str(stored)
    assert "机密采购合同" not in key

    # 但原始文件名必须作为 metadata 保留
    stored_name = _scalar("SELECT file_name FROM contract_file WHERE id = %s", (body["file_id"],))
    assert stored_name == "机密采购合同-2026.docx"


def test_response_does_not_expose_storage_internals(client: TestClient) -> None:
    response = client.post("/api/v1/contracts", files=_files(_docx("sec")), data=_form(_unique_no()))
    assert response.status_code == 201

    raw = response.text
    assert "storage_path" not in raw
    assert "storage_key" not in raw
    assert "uploads" not in raw
    assert "\\\\" not in raw, "响应里不应出现 Windows 路径分隔符"


# --------------------------------------------------------------------------- #
# 4. 参数与类型错误
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("missing", ["contract_no", "title", "contract_type"])
def test_missing_required_field_is_rejected(client: TestClient, missing: str) -> None:
    form = _form(_unique_no())
    form.pop(missing)

    response = client.post("/api/v1/contracts", files=_files(_docx()), data=form)
    assert response.status_code == 422, f"缺少 {missing} 应当被参数校验拦下"


def test_invalid_contract_type_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/contracts",
        files=_files(_docx()),
        data=_form(_unique_no(), contract_type="NOT_A_TYPE"),
    )
    assert response.status_code == 422


def test_unsupported_file_type_returns_415(client: TestClient) -> None:
    response = client.post(
        "/api/v1/contracts",
        files={"file": ("恶意.txt", b"plain text", "text/plain")},
        data=_form(_unique_no()),
    )
    assert response.status_code == 415
    assert response.json()["code"] == "UNSUPPORTED_FORMAT"


def test_spoofed_extension_is_rejected(client: TestClient) -> None:
    """扩展名是 .pdf 但内容是纯文本 —— 魔数校验必须拦下。"""
    response = client.post(
        "/api/v1/contracts",
        files={"file": ("伪装.pdf", b"not a pdf at all", "application/pdf")},
        data=_form(_unique_no()),
    )
    assert response.status_code == 415


def test_oversize_upload_returns_413(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """把上限临时调小，避免真的构造 50MB 请求体（常量本身在单元测试里已断言）。"""
    monkeypatch.setattr(contracts_endpoint, "MAX_UPLOAD_SIZE_BYTES", 100)

    response = client.post("/api/v1/contracts", files=_files(_docx("oversize")), data=_form(_unique_no()))

    assert response.status_code == 413
    assert response.json()["code"] == "FILE_TOO_LARGE"


def test_every_error_response_carries_request_id(client: TestClient) -> None:
    response = client.post(
        "/api/v1/contracts",
        files={"file": ("x.txt", b"x", "text/plain")},
        data=_form(_unique_no()),
    )
    assert response.status_code == 415
    assert response.json()["request_id"] == response.headers["X-Request-ID"]


# --------------------------------------------------------------------------- #
# 5. 内容去重
# --------------------------------------------------------------------------- #
def test_duplicate_content_reuses_everything_and_leaves_no_residue(
    client: TestClient, _isolated_storage: LocalStorageBackend
) -> None:
    """第二次上传同一个文件内容（**换文件名、换 contract_no**）必须完全复用。"""
    payload = _docx("dedup")
    contract_no = _unique_no()

    first = client.post("/api/v1/contracts", files=_files(payload, "原名.docx"), data=_form(contract_no))
    assert first.status_code == 201
    assert first.json()["reused"] is False
    winner = first.json()

    second = client.post(
        "/api/v1/contracts",
        files=_files(payload, "换了个名字.docx"),
        data=_form(_unique_no()),  # 不同的合同编号
    )
    assert second.status_code == 200, "复用应当返回 200 而不是 201"
    loser = second.json()

    assert loser["reused"] is True
    assert loser["task_reused"] is True
    assert loser["contract_id"] == winner["contract_id"]
    assert loser["file_id"] == winner["file_id"]
    assert loser["review_task_id"] == winner["review_task_id"]
    # 复用时返回的是**胜出方**的编号，不是本次请求里传的那个
    assert loser["contract_no"] == contract_no

    # ---- 数据库层面：不得留下孤儿 Contract / 多余附件与任务 ----
    contract_ids = [
        row[0] for row in _query("SELECT id FROM contract WHERE contract_no LIKE %s", (f"{PREFIX}%",))
    ]
    assert contract_ids == [winner["contract_id"]], "不得留下孤儿 Contract"
    assert _scalar("SELECT COUNT(*) FROM contract_file") == 1
    assert _scalar("SELECT COUNT(*) FROM review_task") == 1

    # ---- 文件层面：输方的目录必须被清理（连同空目录）----
    uploads = _isolated_storage.root / "uploads"
    directories = [d for d in uploads.iterdir() if d.is_dir()]
    assert [d.name for d in directories] == [str(winner["contract_id"])], "输方目录未被清理"
    assert len(list(directories[0].iterdir())) == 1


def test_same_contract_no_with_different_content_is_conflict_not_dedup(
    client: TestClient, _isolated_storage: LocalStorageBackend
) -> None:
    """**合同编号冲突不能被误判成文件去重** —— 两者是完全不同的语义。"""
    contract_no = _unique_no()

    first = client.post("/api/v1/contracts", files=_files(_docx("first")), data=_form(contract_no))
    assert first.status_code == 201

    # 内容不同（sha256 不同）但合同编号相同 ⇒ 必须 409，而不是 200 复用
    second = client.post("/api/v1/contracts", files=_files(_docx("second")), data=_form(contract_no))

    assert second.status_code == 409, second.text
    assert second.json()["code"] == "CONFLICT"
    assert second.json()["details"]["contract_no"] == contract_no

    # 失败的上传不得留下任何多余记录，也不得留下文件
    assert _scalar("SELECT COUNT(*) FROM contract WHERE contract_no = %s", (contract_no,)) == 1
    assert _scalar("SELECT COUNT(*) FROM contract_file") == 1

    uploads = _isolated_storage.root / "uploads"
    for directory in uploads.iterdir():
        if directory.is_dir():
            assert len(list(directory.iterdir())) == 1, f"失败请求留下了文件：{directory}"


# --------------------------------------------------------------------------- #
# 8. 边界与既有约束
# --------------------------------------------------------------------------- #
def test_foreign_keys_remain_restrict(client: TestClient) -> None:
    """P3 的 FK 无级联约定必须保持不变：删父记录会被数据库拒绝。"""
    response = client.post("/api/v1/contracts", files=_files(_docx("fk")), data=_form(_unique_no()))
    assert response.status_code == 201
    contract_id = response.json()["contract_id"]

    with pytest.raises(pymysql.err.IntegrityError):
        _query("DELETE FROM contract WHERE id = %s", (contract_id,))


def test_seed_rules_are_not_polluted_by_upload_tests(client: TestClient) -> None:
    """P3 seed 的 3 条规则属于基础数据，上传测试不得增删改。"""
    before = _scalar("SELECT COUNT(*) FROM review_rule")
    client.post("/api/v1/contracts", files=_files(_docx("seed")), data=_form(_unique_no()))
    after = _scalar("SELECT COUNT(*) FROM review_rule")

    assert before == after == 3, f"seed 规则数量被改变：{before} -> {after}"


# --------------------------------------------------------------------------- #
# 9. 任务级幂等：review_task.idempotency_key
#
# 两层幂等互相独立：
#   文件层 = contract_file.sha256   → 响应字段 reused
#   任务层 = review_task.idempotency_key → 响应字段 task_reused
#      键 = sha256(contract_id ‖ file_sha256 ‖ rule_set_version ‖ prompt_version)
#
# 任务层**不看 status**：同一套审查配置就对应同一个任务；
# 要重新审查必须换配置（新规则集版本 / 新 prompt 版本）⇒ 新键 ⇒ 新任务。
# --------------------------------------------------------------------------- #
RULE_SET_PREFIX = "IT-RULESET-"


def _active_rule_set_version() -> str:
    return _scalar(
        "SELECT version FROM review_rule_set WHERE contract_type = 'PURCHASE' AND is_active = 1 "
        "ORDER BY id DESC LIMIT 1"
    )


@pytest.fixture
def add_rule_set() -> Iterator[Callable[..., int]]:
    """插入一条 PURCHASE 规则集供用例使用，结束后删除（不污染 P3 seed）。

    ``_active_rule_set_version`` 取 ``ORDER BY id DESC LIMIT 1``，
    因此新插入的这条会立即成为生效版本。
    """
    created: list[int] = []

    def _add(version: str, *, is_active: bool = True) -> int:
        _query(
            "INSERT INTO review_rule_set "
            "(name, contract_type, version, is_active, created_at, updated_at) "
            "VALUES (%s, 'PURCHASE', %s, %s, NOW(3), NOW(3))",
            (f"{RULE_SET_PREFIX}{version}", version, 1 if is_active else 0),
        )
        rule_set_id = _scalar(
            "SELECT id FROM review_rule_set WHERE name = %s ORDER BY id DESC LIMIT 1",
            (f"{RULE_SET_PREFIX}{version}",),
        )
        created.append(rule_set_id)
        return rule_set_id

    yield _add

    for rule_set_id in created:
        _query("DELETE FROM review_rule_set WHERE id = %s", (rule_set_id,))


def _set_rule_set_active(rule_set_id: int, is_active: bool) -> None:
    _query(
        "UPDATE review_rule_set SET is_active = %s WHERE id = %s",
        (1 if is_active else 0, rule_set_id),
    )


def _upload(client: TestClient, payload: bytes) -> Any:
    """用**全新的合同编号**上传同一份内容 —— 合同编号不参与任何一层的幂等判断。"""
    return client.post("/api/v1/contracts", files=_files(payload), data=_form(_unique_no()))


def _task_count(contract_id: int) -> int:
    return _scalar("SELECT COUNT(*) FROM review_task WHERE contract_id = %s", (contract_id,))


# ------------------------------- T1 / T11 ------------------------------- #
def test_idempotency_key_matches_the_documented_formula(client: TestClient) -> None:
    """T1 补充：首次上传时落库的幂等键必须等于文档公式算出来的值。"""
    payload = _docx("t1")
    response = _upload(client, payload)
    assert response.status_code == 201
    body = response.json()

    assert body["reused"] is False
    assert body["task_reused"] is False

    expected = build_idempotency_key(
        body["contract_id"],
        body["sha256"],
        _active_rule_set_version(),
        contract_ingest.INGEST_ENGINE_VERSION,
    )
    actual = _scalar("SELECT idempotency_key FROM review_task WHERE id = %s", (body["review_task_id"],))
    assert actual == expected


def test_latest_task_helper_is_gone() -> None:
    """T11：任务复用只能由幂等键精确决定 —— "id 最大" 这种任意代理必须已删除。"""
    assert not hasattr(contract_ingest, "_latest_task")


def test_reused_flag_means_an_existing_contract_file_was_reused(client: TestClient) -> None:
    """T11：reused=True 必须确实对应"复用了已存在的 ContractFile"。"""
    payload = _docx("t11")
    first = _upload(client, payload).json()
    second = _upload(client, payload)

    body = second.json()
    assert body["reused"] is True
    assert body["file_id"] == first["file_id"]
    assert _scalar("SELECT COUNT(*) FROM contract_file WHERE sha256 = %s", (body["sha256"],)) == 1


# ---------------------------------- T3 ---------------------------------- #
def test_rule_version_change_creates_a_second_task_on_the_same_file(
    client: TestClient, add_rule_set: Callable[..., int]
) -> None:
    """T3：同文件 + 规则版本变化 ⇒ 复用文件，但**新建** ReviewTask。"""
    payload = _docx("t3")
    first = _upload(client, payload)
    assert first.status_code == 201
    first_body = first.json()
    assert _task_count(first_body["contract_id"]) == 1

    add_rule_set("v2")  # 新版本立即生效

    second = _upload(client, payload)

    assert second.status_code == 201, "创建了新审查任务 ⇒ 201（不是 200）"
    body = second.json()
    assert body["reused"] is True, "文件仍然复用"
    assert body["task_reused"] is False, "审查配置变了 ⇒ 新建任务"
    assert body["contract_id"] == first_body["contract_id"]
    assert body["file_id"] == first_body["file_id"]
    assert body["review_task_id"] != first_body["review_task_id"]

    assert _task_count(body["contract_id"]) == 2
    assert _scalar("SELECT COUNT(*) FROM contract_file") == 1
    # 新建的任务成为合同当前任务
    assert (
        _scalar("SELECT current_task_id FROM contract WHERE id = %s", (body["contract_id"],))
        == (body["review_task_id"])
    )


# ---------------------------------- T4 ---------------------------------- #
def test_prompt_version_change_creates_a_second_task(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T4：同文件 + prompt/审查配置版本变化 ⇒ 新键 ⇒ 新建 ReviewTask。"""
    payload = _docx("t4")
    first_body = _upload(client, payload).json()

    monkeypatch.setattr(contract_ingest, "INGEST_ENGINE_VERSION", "ingest-v2")

    second = _upload(client, payload)

    assert second.status_code == 201
    body = second.json()
    assert body["reused"] is True
    assert body["task_reused"] is False
    assert body["review_task_id"] != first_body["review_task_id"]

    keys = {
        row[0]
        for row in _query(
            "SELECT idempotency_key FROM review_task WHERE contract_id = %s", (body["contract_id"],)
        )
    }
    assert len(keys) == 2, "两份审查配置必须产生两个不同的幂等键"


# ------------------------------ T5 / T6 / T7 ---------------------------- #
@pytest.mark.parametrize("task_status", ["pending", "parsing", "reviewing", "blocked", "completed"])
def test_task_reuse_ignores_status(client: TestClient, task_status: str) -> None:
    """T5/T6/T7：任务层幂等只看审查配置，**不看 status**。

    `completed` 是终态的含义是"不能改它"，不是"不能返回它"——
    同配置再次上传应当原样复用，既不复活也不新建。
    """
    payload = _docx(f"status-{task_status}")
    first_body = _upload(client, payload).json()

    _query(
        "UPDATE review_task SET status = %s WHERE id = %s",
        (task_status, first_body["review_task_id"]),
    )

    second = _upload(client, payload)

    assert second.status_code == 200
    body = second.json()
    assert body["reused"] is True
    assert body["task_reused"] is True
    assert body["review_task_id"] == first_body["review_task_id"]
    assert body["task_status"] == task_status, "复用不得改变任务状态"
    assert _task_count(first_body["contract_id"]) == 1, "不得新建任务"


# ---------------------------------- T8 ---------------------------------- #
def test_rule_version_rollback_reuses_the_original_task(
    client: TestClient, add_rule_set: Callable[..., int]
) -> None:
    """T8：v1 → （v2 生效）→ v2 停用 ⇒ 键回到 v1 ⇒ 重新命中**最初那个**任务。

    同时验证 `current_task_id` 不会因为复用旧任务而倒退。
    """
    payload = _docx("t8")
    v1_body = _upload(client, payload).json()

    v2_id = add_rule_set("v2")
    v2_body = _upload(client, payload).json()
    assert v2_body["task_reused"] is False
    assert v2_body["review_task_id"] != v1_body["review_task_id"]
    assert _task_count(v1_body["contract_id"]) == 2

    _set_rule_set_active(v2_id, False)  # v2 停用 ⇒ 回落到 v1

    third = _upload(client, payload)

    assert third.status_code == 200, "回到 v1 ⇒ 命中既有任务，本次没有创建任何东西"
    body = third.json()
    assert body["reused"] is True
    assert body["task_reused"] is True
    assert body["review_task_id"] == v1_body["review_task_id"], "必须重新命中最初的 v1 任务"
    assert _task_count(v1_body["contract_id"]) == 2, "不得产生第三个任务"
    assert (
        _scalar("SELECT current_task_id FROM contract WHERE id = %s", (v1_body["contract_id"],))
        == (v2_body["review_task_id"])
    ), "复用旧任务不得让 current_task_id 倒退"


# ---------------------------------- T10 --------------------------------- #
def test_same_content_different_metadata_reuses_both_layers(client: TestClient) -> None:
    """T10：文件名与合同编号都不参与任何一层的幂等判断。"""
    payload = _docx("t10")
    first_body = _upload(client, payload).json()

    second = client.post(
        "/api/v1/contracts",
        files=_files(payload, "完全不同的名字.docx"),
        data=_form(_unique_no(), title="完全不同的标题"),
    )

    assert second.status_code == 200
    body = second.json()
    assert body["reused"] is True
    assert body["task_reused"] is True
    assert body["contract_id"] == first_body["contract_id"]
    assert body["file_id"] == first_body["file_id"]
    assert body["review_task_id"] == first_body["review_task_id"]
    assert _task_count(first_body["contract_id"]) == 1

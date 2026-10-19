"""场景编排端点。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/scenes/preview` | **自然语言 → 动作序列 + 冲突检测**（不执行、不保存） |
| POST | `/api/scenes/plan` | 对手动微调过的动作列表重新编排（前端"改完再看冲突"） |
| POST | `/api/scenes` | 保存场景 |
| GET | `/api/scenes` | 场景列表 |
| GET | `/api/scenes/{scene_id}` | 场景详情 |
| PUT | `/api/scenes/{scene_id}` | 改名 / 改描述 / 改动作（重新校验） |
| PATCH | `/api/scenes/{scene_id}/favorite` | 收藏 / 取消收藏 |
| DELETE | `/api/scenes/{scene_id}` | 删除场景（连同执行记录） |
| POST | `/api/scenes/{scene_id}/run` | 执行（高风险步骤只返回待确认） |
| GET | `/api/scenes/runs/{run_id}` | 执行记录（含被剔除步骤与失败步骤） |

关于预览与执行的分离
------------------
`preview` 与 `plan` **只算不做**：它们会跑完整的"能力校验 + 冲突检测"，
但**不下发任何指令、不写任何记录**。用户看清楚了再点执行 ——
这条界线让"生成一个场景"变成可反复试错的过程，而不是一按就动设备。

`run` 才真正下发，并且**高危动作永远不会出现在下发路径上**（见 executor）。

多套房下的口径
--------------
所有会挑设备的端点都先把全量目录 `DeviceCatalog` **收窄到当前套房**
（`_scoped()` → `catalog.for_home()`），再交给 nl / planner / executor：

- `preview` / `plan` 用请求里的 `home_id`
- `create` 用 `body.home_id`，`update` / `run` 用 `scene.home_id`

收窄是在**目录层**做的，所以给模型看的设备清单（`nl.render_device_catalog`）
也只剩这一套房的 —— 模型不会凭空编出别套房的设备码。
`home_id` 为 None 时不收窄（旧调用方与回归脚本的行为）。
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.api.deps import get_catalog
from app.db.catalog import DeviceCatalog
from app.db.session import get_db
from app.models.orm import Scene, SceneRun
from app.schemas.scene import (
    SceneActionOut,
    SceneCreateIn,
    SceneFavoriteIn,
    SceneOut,
    ScenePlanIn,
    ScenePlanOut,
    ScenePreviewIn,
    ScenePreviewOut,
    SceneRunOut,
    SceneStepIn,
    SceneUpdateIn,
)
from app.services.scenes import executor, nl, planner
from app.services.scenes.models import RunResult, ScenePlan

router = APIRouter(prefix="/scenes", tags=["scenes"])


# ---------------------------------------------------------------- 转换工具
def _actions_out(outcomes) -> list[SceneActionOut]:
    return [SceneActionOut(**o.as_dict()) for o in outcomes]


def _plan_payload(plan: ScenePlan) -> dict:
    return {
        "intent_key": plan.intent_key,
        "intent_label": plan.intent_label,
        "raw_count": plan.raw_count,
        "action_count": len(plan.actions),
        "rejected_count": len(plan.rejected),
        "executable_count": plan.executable_count,
        "pending_confirm_count": plan.pending_confirm_count,
        "actions": _actions_out(plan.actions),
        "rejected": _actions_out(plan.rejected),
        "notes": plan.notes,
    }


def _scoped(catalog: DeviceCatalog, home_id: int | None) -> DeviceCatalog:
    """把全量目录收窄到某一套房。

    编排链路（nl / planner / executor）全程只认 `DeviceCatalog`，
    在这里收窄一次，后面所有环节看到的就都是这一套房的设备，不会串台。

    `home_id` 为 None 时原样返回 —— 那是旧调用方与回归脚本的行为，
    新代码一律要传；多套房下不传等于"在所有套房里挑设备"。
    """
    return catalog if home_id is None else catalog.for_home(home_id)


def _scene_out(scene: Scene) -> SceneOut:
    """ORM → 接口模型。

    `favorite` 与动作都存在 `definition_json` 里：`scenes` 表没有对应列，
    而 `app_rw` 账号**没有 DDL 权限**（建表/加列都在虚拟机上执行），
    所以不走 ALTER TABLE，直接用现成的 JSON 列。
    """
    definition = scene.definition_json or {}
    actions = definition.get("actions") or []
    return SceneOut(
        id=scene.id,
        home_id=scene.home_id,
        name=scene.name,
        description=str(definition.get("description") or ""),
        trigger_type=scene.trigger_type,
        action_count=len(actions),
        intent_key=definition.get("intent_key"),
        enabled=bool(scene.enabled),
        favorite=bool(definition.get("favorite")),
        actions=[SceneStepIn(**a) for a in actions],
        created_at=scene.created_at,
    )


def _run_payload(result: RunResult) -> dict:
    return {
        "status": result.status,
        "scene_id": result.scene_id,
        "run_id": result.run_id,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "executed_count": len(result.executed),
        "pending_confirm_count": len(result.pending_confirm),
        "failed_count": len(result.failed),
        "fallback_used": result.fallback_used,
        "executed": _actions_out(result.executed),
        "pending_confirm": _actions_out(result.pending_confirm),
        "failed": _actions_out(result.failed),
        "notes": result.notes,
    }


# ---------------------------------------------------------------- 预览
@router.post(
    "/preview",
    response_model=ScenePreviewOut,
    summary="自然语言 → 动作序列 + 冲突检测（不执行）",
)
def preview(
    body: ScenePreviewIn,
    catalog: DeviceCatalog = Depends(get_catalog),
):
    """这是 M3 的核心端点：**先查能力档案，再做冲突检测**。

    注意这里是 `def`（同步）—— LLM 调用与后续编排都是阻塞的，
    写成 `async def` 会卡住整个后端。

    ⚠️ `home_id` 决定这次编排**能看到哪些设备**：收窄后的目录会直接喂给模型
    （`render_device_catalog`），所以模型压根看不到别套房的设备，
    也就不会编排出"在 2 号房点按钮、动作却落在 1 号房设备上"的结果。
    """
    catalog = _scoped(catalog, body.home_id)
    if body.home_id is not None and not len(catalog):
        # 空目录喂给模型只会让它凭空编设备码，回来全被剔除 —— 不如当场说清楚
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"套房 {body.home_id} 下没有登记任何启用中的设备，无法编排",
        )

    try:
        steps, understanding, raw_output = nl.build_scene_from_text(catalog, body.query)
    except nl.SceneBuildError as e:
        # 模型不可用 / 输出无法解析：如实报错，不返回一个"看起来像成功"的空计划
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e

    plan = planner.plan_scene(
        catalog, steps, scene_name=body.query, description=understanding
    )

    payload = _plan_payload(plan)
    payload.update(
        {
            "query": body.query,
            "understanding": understanding,
            "model_output": raw_output,
        }
    )

    if body.dry_run_execute:
        home_id = body.home_id if body.home_id is not None else (
            catalog.home_ids()[0] if catalog.home_ids() else 1
        )
        result = executor.execute_plan(
            plan, catalog, db=None, home_id=home_id, dry_run=True
        )
        payload["notes"] = list(payload["notes"]) + result.notes

    return ScenePreviewOut(**payload)


@router.post(
    "/plan",
    response_model=ScenePlanOut,
    summary="对手动调整过的动作列表重新编排（不执行）",
)
def plan_actions(
    body: ScenePlanIn,
    catalog: DeviceCatalog = Depends(get_catalog),
):
    # 与 preview 同一套房口径：不收窄的话，手动挑的别套房设备也会被判成"合法动作"
    catalog = _scoped(catalog, body.home_id)
    plan = planner.plan_scene(
        catalog,
        body.actions,
        scene_name=body.scene_name,
        description=body.description,
    )
    return ScenePlanOut(**_plan_payload(plan))


# ---------------------------------------------------------------- 场景 CRUD
@router.post("", response_model=SceneOut, summary="保存场景")
def create_scene(
    body: SceneCreateIn,
    db: Session = Depends(get_db),
    catalog: DeviceCatalog = Depends(get_catalog),
):
    # 场景挂在哪套房，就只在那套房的设备里校验 ——
    # 否则会把属于别套房的动作存进这个场景，执行时才发现设备不在这一套。
    catalog = _scoped(catalog, body.home_id)

    # 保存前先编排一遍：**只把通过校验的动作存进去**。
    # 否则库里会攒下一堆"动作写着但设备不存在/能力不存在"的僵尸场景，
    # 等到执行时才发现，那就太晚了。
    plan = planner.plan_scene(
        catalog, body.actions, scene_name=body.name, description=body.description
    )
    if not plan.actions:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {
                "message": "没有通过校验的动作，场景未保存",
                "rejected": [r.as_dict() for r in plan.rejected],
            },
        )

    definition = {
        "intent_key": plan.intent_key,
        "intent_label": plan.intent_label,
        "description": body.description,
        "actions": [
            {"device_code": a.device_code, "capability": a.capability, "value": a.value}
            for a in plan.actions
        ],
        "rejected_at_save": [r.as_dict() for r in plan.rejected],
        "notes": plan.notes,
    }

    scene = Scene(
        home_id=body.home_id,
        name=body.name,
        trigger_type=body.trigger_type
        if body.trigger_type in ("manual", "schedule", "sensor", "voice")
        else "manual",
        definition_json=definition,
        enabled=body.enabled,
    )
    db.add(scene)
    db.commit()
    db.refresh(scene)
    return _scene_out(scene)


@router.get("", response_model=list[SceneOut], summary="场景列表")
def list_scenes(
    home_id: int | None = Query(None),
    db: Session = Depends(get_db),
):
    stmt = select(Scene).order_by(Scene.id.desc())
    if home_id is not None:
        stmt = stmt.where(Scene.home_id == home_id)
    scenes = db.execute(stmt).scalars().all()
    return [_scene_out(s) for s in scenes]


@router.get("/runs/{run_id}", response_model=SceneRunOut, summary="执行记录详情")
def get_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(SceneRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"执行记录 {run_id} 不存在")
    steps = run.steps_json or {}
    return SceneRunOut(
        status=run.status,
        scene_id=run.scene_id,
        run_id=run.id,
        started_at=run.started_at,
        finished_at=run.finished_at,
        executed_count=len(steps.get("executed") or []),
        pending_confirm_count=len(steps.get("pending_confirm") or []),
        failed_count=len(steps.get("failed") or []),
        fallback_used=bool(run.fallback_used),
        executed=[SceneActionOut(**x) for x in (steps.get("executed") or [])],
        pending_confirm=[SceneActionOut(**x) for x in (steps.get("pending_confirm") or [])],
        failed=[SceneActionOut(**x) for x in (steps.get("failed") or [])],
        notes=[f"被剔除的步骤 {len(steps.get('rejected') or [])} 条"] if steps.get("rejected") else [],
    )


@router.get("/{scene_id}", response_model=SceneOut, summary="场景详情")
def get_scene(scene_id: int, db: Session = Depends(get_db)):
    scene = db.get(Scene, scene_id)
    if scene is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"场景 {scene_id} 不存在")
    return _scene_out(scene)


# ---------------------------------------------------------------- 编辑 / 收藏 / 删除
@router.put("/{scene_id}", response_model=SceneOut, summary="编辑场景（改名 / 改描述 / 改动作）")
def update_scene(
    scene_id: int,
    body: SceneUpdateIn,
    db: Session = Depends(get_db),
    catalog: DeviceCatalog = Depends(get_catalog),
):
    """改动作时会**重新走一遍编排校验**，和新建时同一套规则。

    校验不过的动作不会静默丢掉：这条请求会整体 422 并列出被剔除的动作与原因，
    用户在弹窗里能直接看到"我刚加的那条为什么不行"，而不是保存成功后发现少了一条。
    """
    scene = db.get(Scene, scene_id)
    if scene is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"场景 {scene_id} 不存在")

    # 按场景所属的套房校验：换一套房就不能拿那套的设备来改这一套的场景
    catalog = _scoped(catalog, scene.home_id)

    # ⚠️ JSON 列必须**整体重新赋值**：SQLAlchemy 不追踪 dict 内部变更，
    #    原地改 definition["favorite"] 不会生成 UPDATE。
    definition = dict(scene.definition_json or {})

    if body.name is not None:
        scene.name = body.name
    if body.enabled is not None:
        scene.enabled = body.enabled
    if body.favorite is not None:
        definition["favorite"] = body.favorite

    if body.description is not None:
        definition["description"] = body.description

    if body.actions is not None:
        if not body.actions:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "场景至少要有一个动作 —— 空场景执行时只会报错",
            )
        plan = planner.plan_scene(
            catalog,
            body.actions,
            scene_name=scene.name,
            description=str(definition.get("description") or ""),
        )
        if not plan.actions:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                {
                    "message": "没有通过校验的动作，改动未保存",
                    "rejected": [r.as_dict() for r in plan.rejected],
                },
            )
        definition["actions"] = [
            {"device_code": a.device_code, "capability": a.capability, "value": a.value}
            for a in plan.actions
        ]
        definition["intent_key"] = plan.intent_key
        definition["intent_label"] = plan.intent_label
        definition["notes"] = plan.notes
        definition["rejected_at_save"] = [r.as_dict() for r in plan.rejected]

    scene.definition_json = definition
    db.commit()
    db.refresh(scene)
    return _scene_out(scene)


@router.patch("/{scene_id}/favorite", response_model=SceneOut, summary="收藏 / 取消收藏")
def set_favorite(scene_id: int, body: SceneFavoriteIn, db: Session = Depends(get_db)):
    scene = db.get(Scene, scene_id)
    if scene is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"场景 {scene_id} 不存在")
    definition = dict(scene.definition_json or {})
    definition["favorite"] = body.favorite
    scene.definition_json = definition
    db.commit()
    db.refresh(scene)
    return _scene_out(scene)


@router.delete("/{scene_id}", summary="删除场景（连同执行记录）")
def delete_scene(scene_id: int, db: Session = Depends(get_db)):
    """物理删除。

    ⚠️ 必须先删 `scene_runs`：`scene_runs.scene_id` 是外键且没有 ON DELETE CASCADE，
    直接删场景会被 MySQL 用 1451 挡回来。
    返回体里如实报删掉多少条执行记录 —— 这也是**不可恢复**的。
    """
    scene = db.get(Scene, scene_id)
    if scene is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"场景 {scene_id} 不存在")

    runs = db.execute(delete(SceneRun).where(SceneRun.scene_id == scene_id)).rowcount or 0
    name = scene.name
    db.delete(scene)
    db.commit()
    return {"deleted": True, "id": scene_id, "name": name, "runs_deleted": runs}


# ---------------------------------------------------------------- 执行
@router.post("/{scene_id}/run", response_model=SceneRunOut, summary="执行场景（高风险步骤返回待确认）")
def run_scene(
    scene_id: int,
    db: Session = Depends(get_db),
    catalog: DeviceCatalog = Depends(get_catalog),
):
    scene = db.get(Scene, scene_id)
    if scene is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"场景 {scene_id} 不存在")
    if not scene.enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, f"场景「{scene.name}」已被禁用，不执行")

    definition = scene.definition_json or {}
    stored_actions = definition.get("actions") or []
    if not stored_actions:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "该场景没有任何动作")

    # 执行前**重新编排一遍**：设备目录与能力档案可能已经变了
    # （比如设备被删了）。存下来的动作不能假设永远有效。
    # 目录按场景所属套房收窄：执行路径上更不能碰到别套房的设备。
    catalog = _scoped(catalog, scene.home_id)
    plan = planner.plan_scene(
        catalog,
        stored_actions,
        scene_name=scene.name,
        description=str(definition.get("description") or ""),
    )
    if not plan.actions:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {
                "message": "场景中的动作在当前设备清单下全部无法执行",
                "rejected": [r.as_dict() for r in plan.rejected],
            },
        )

    result = executor.execute_plan(
        plan,
        catalog,
        db=db,
        home_id=scene.home_id,
        issued_by=f"scene:{scene.id}",
    )
    result.scene_id = scene.id
    executor.save_run(db, scene_id=scene.id, result=result, plan=plan)
    return SceneRunOut(**_run_payload(result))

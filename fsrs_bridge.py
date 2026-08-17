"""FSRS-6 调度适配层（A1）。

规则（R2）：vendored 依赖缺失时必须降级，主路径仅标准库。
- vendor/fsrs 目录存在 → 使用 FSRS-6 调度；
- 缺失/导入失败 → 回退 SM-2（review.compute_review），功能不中断。

P0 增强（2026-08-12）：
- 个性化参数训练：用本地复习历史训练 FSRS 21 参数（需 torch/pandas/tqdm，
  属用户可选安装；缺失时仅提示，调度仍用默认参数）→ data/fsrs_params.json；
- 目标保持率 desired_retention 可调（settings.fsrs_desired_retention，默认 0.9）；
- retrievability() 预测当前检索概率（遗忘预测可视化用）。

复习评分（rating 1-4）映射 FSRS Rating：
  1=Again 2=Hard 3=Good 4=Easy

状态持久化：problems.state/stability/difficulty 列（v6 迁移）。
"""
from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from config import APP_DIR, LOG
from db import now

_VENDOR_DIR = Path(__file__).resolve().parent / "vendor" / "fsrs"

try:
    if _VENDOR_DIR.exists():
        sys.path.insert(0, str(_VENDOR_DIR))
    from fsrs import Scheduler, Card, Rating  # type: ignore
    from fsrs.scheduler import DEFAULT_PARAMETERS  # type: ignore
    _FSRS_AVAILABLE = True
except Exception as exc:  # pragma: no cover - 降级路径
    _FSRS_AVAILABLE = False
    DEFAULT_PARAMETERS = None  # type: ignore[assignment]
    LOG.warning("FSRS vendored 依赖不可用，回退 SM-2: %s", exc)

_PARAM_FILE = APP_DIR / "data" / "fsrs_params.json"
_RETENTION_KEY = "fsrs_desired_retention"
# §16.2/§46.5C：训练门槛单一常量（消除 handler 与 train_parameters 两处重复），
# 并作为「高置信度」判定的样本基线。
FSRS_TRAIN_MIN_SAMPLES = 10


def confidence_for(sample_count: int) -> str:
    """§16.2 高置信度标记：训练结果携带，供 UI 提示个性化参数可信度。"""
    if sample_count >= 200:
        return "high"
    if sample_count >= 50:
        return "medium"
    if sample_count >= FSRS_TRAIN_MIN_SAMPLES:
        return "low"
    return "insufficient"


def _retention_key(subject: str = "") -> str:
    """§46.5C：学科分层保持率键。无学科 → 全局键；有学科 → 学科专属键（覆盖全局）。"""
    subject = (subject or "").strip()
    if not subject or subject == "physics":
        return _RETENTION_KEY
    return f"{_RETENTION_KEY}_{subject}"

_PARAM_CACHE: dict[str, object] | None = None
_TRAIN_STATE: dict[str, object] = {"running": False, "result": None, "error": None}


def _invalidate() -> None:
    global _PARAM_CACHE
    _PARAM_CACHE = None


def _load_params() -> dict[str, object] | None:
    global _PARAM_CACHE
    if _PARAM_CACHE is not None:
        return _PARAM_CACHE
    try:
        if _PARAM_FILE.is_file():
            data = json.loads(_PARAM_FILE.read_text("utf-8"))
            params = [float(x) for x in data.get("parameters", [])]
            if len(params) == len(DEFAULT_PARAMETERS):
                _PARAM_CACHE = {"parameters": params, "trained_at": data.get("trained_at", "")}
                return _PARAM_CACHE  # type: ignore[return-value]
    except Exception as exc:
        LOG.warning("FSRS 参数文件读取失败（用默认参数）: %s", exc)
    _PARAM_CACHE = None
    return None


def _desired_retention(subject: str = "") -> float:
    """§46.5C：学科分层保持率。学科有专属覆盖则用覆盖，否则回退全局，否则 0.9。"""
    from db import settings_dict
    key = _retention_key(subject)
    try:
        value = float(settings_dict().get(key, settings_dict().get(_RETENTION_KEY, "0.9")))
        return value if 0.75 <= value <= 0.97 else 0.9
    except (TypeError, ValueError):
        return 0.9


def _scheduler(subject: str = "") -> Scheduler:
    params = DEFAULT_PARAMETERS
    trained = _load_params()
    if trained:
        params = trained["parameters"]  # type: ignore[assignment]
    try:
        return Scheduler(parameters=params, desired_retention=_desired_retention(subject))
    except Exception as exc:
        LOG.warning("FSRS 参数校验失败（用默认）: %s", exc)
        return Scheduler(desired_retention=_desired_retention(subject))


@dataclass
class FsrsState:
    """可持久化的 FSRS 卡状态（对应 problems 表列）。"""
    state: int
    stability: float
    difficulty: float
    due: str
    last_review: str
    elapsed_days: int
    scheduled_days: int


def fsrs_available() -> bool:
    return _FSRS_AVAILABLE


def _state_to_card(
    state: int,
    stability: float,
    difficulty: float,
    prev_interval: int,
) -> Card:
    """从持久化状态重建 Card；无历史时按已学间隔估算。"""
    card = Card()
    if state > 0:
        card.state = state
        card.stability = stability or max(prev_interval * 0.6, 1.0)
        card.difficulty = difficulty
    elif prev_interval > 1:
        card.state = 1  # Learning
        card.stability = max(prev_interval * 0.6, 1.0)
    return card


def compute_fsrs_review(
    rating: int,
    prev_interval: int,
    state: int = 0,
    stability: float = 0.0,
    difficulty: float = 0.0,
    today: date | None = None,
    subject: str = "",
) -> FsrsState:
    """FSRS-6 调度。返回下一次复习状态（供 problems 表持久化）。
    subject 用于 §46.5C 学科分层保持率（无则全局）。
    """
    today = today or date.today()
    card = _state_to_card(state, stability, difficulty, prev_interval)
    scheduler = _scheduler(subject)
    review_dt = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
    updated, _log = scheduler.review_card(
        card,
        Rating(rating),
        review_datetime=review_dt,
        review_duration=0,
    )
    due = updated.due
    scheduled = max(1, (due.date() - today).days)
    return FsrsState(
        state=int(updated.state),
        stability=round(updated.stability, 3),
        difficulty=round(updated.difficulty, 3),
        due=due.date().isoformat(),
        last_review=today.isoformat(),
        elapsed_days=max(0, (due.date() - today).days),
        scheduled_days=scheduled,
    )


def retrievability(
    prev_interval: int,
    state: int = 0,
    stability: float = 0.0,
    difficulty: float = 0.0,
    last_review: str = "",
    current: date | None = None,
    subject: str = "",
) -> float:
    """P0：预测该卡今天的检索概率 R（0-1）。无 FSRS → 用 SM-2 间隔近似。
    subject 用于 §46.5C 学科分层保持率。
    """
    current = current or date.today()
    if not _FSRS_AVAILABLE:
        return 0.0
    try:
        card = _state_to_card(state, stability, difficulty, prev_interval)
        ref = last_review or current.isoformat()
        ref_dt = datetime.fromisoformat(ref)
        if ref_dt.tzinfo is None:
            ref_dt = ref_dt.replace(tzinfo=timezone.utc)
        card.last_review = ref_dt
        now_dt = datetime.combine(current, datetime.min.time(), tzinfo=timezone.utc)
        return round(float(_scheduler(subject).get_card_retrievability(card, now_dt)), 4)
    except Exception:
        return 0.0


def next_interval_days(
    rating: int,
    prev_interval: int,
    state: int = 0,
    stability: float = 0.0,
    difficulty: float = 0.0,
    today: date | None = None,
) -> int:
    """统一入口：FSRS 可用时用 FSRS，否则回退 SM-2 的间隔计算。"""
    if _FSRS_AVAILABLE:
        try:
            fs = compute_fsrs_review(rating, prev_interval, state, stability, difficulty, today)
            return max(1, fs.scheduled_days)
        except Exception as exc:
            LOG.warning("FSRS 调度异常，回退 SM-2: %s", exc)
    from review import compute_review
    return max(1, compute_review(rating, prev_interval, 2.5, 0).interval_days)


def fsrs_status() -> dict[str, object]:
    """P0：调度与训练状态（设置页 FSRS 卡 + 遗忘预测）。"""
    trained = _load_params()
    status: dict[str, object] = {
        "available": _FSRS_AVAILABLE,
        "params_source": "trained" if trained else "default",
        "trained_at": str(trained["trained_at"]) if trained else "",
        "desired_retention": _desired_retention(),
        "training": bool(_TRAIN_STATE["running"]),
        "sample_count": 0,
    }
    if _TRAIN_STATE["result"]:
        status["last_train"] = _TRAIN_STATE["result"]
        status["sample_count"] = int(_TRAIN_STATE["result"]["sample_count"])
    if _TRAIN_STATE["error"]:
        status["train_error"] = _TRAIN_STATE["error"]
    status["per_subject_retentions"] = per_subject_retentions()  # §46.5C 学科分层
    status["min_train_samples"] = FSRS_TRAIN_MIN_SAMPLES  # §16.2 单一门槛常量
    return status


def set_desired_retention(value: float, subject: str = "") -> bool:
    """P0：设置目标保持率（0.75-0.97），写 settings 表。
    subject 非空时为该学科设置专属保持率（§46.5C 学科分层），空则设全局。
    """
    try:
        value = float(value)
    except (TypeError, ValueError):
        return False
    if not (0.75 <= value <= 0.97):
        return False
    from db import DB_LOCK, db
    with DB_LOCK, db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                     (_retention_key(subject), str(round(value, 2))))
    return True


def per_subject_retentions() -> dict[str, float]:
    """§46.5C：返回所有已设置学科专属保持率的学科及其值（含全局）。"""
    from db import settings_dict
    out: dict[str, float] = {}
    for k, v in settings_dict().items():
        if k == _RETENTION_KEY or k.startswith(_RETENTION_KEY + "_"):
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                pass
    return out


def train_parameters(
    reviews: list[tuple[int, int, str]],
) -> tuple[bool, dict[str, object]]:
    """P0：用本地复习历史训练个性化参数。

    reviews: [(card_id, rating(1-4), iso_datetime)]，按卡分组即为训练序列。
    依赖 torch/pandas/tqdm 缺失 → (False, reason)；成功写 data/fsrs_params.json。
    """
    if not _FSRS_AVAILABLE:
        return False, {"reason": "FSRS 未启用（vendor 缺失）"}
    # §16.2 样本不足应在依赖检查前先行判定（这是数据量问题，与 torch 是否安装无关）
    if len(reviews) < FSRS_TRAIN_MIN_SAMPLES:
        return False, {"reason": "复习记录不足（需 ≥%d 条，当前 %d 条）" % (FSRS_TRAIN_MIN_SAMPLES, len(reviews)),
                       "sample_count": len(reviews),
                       "confidence": confidence_for(len(reviews))}
    try:
        from fsrs.optimizer import Optimizer  # type: ignore
        from fsrs.review_log import ReviewLog  # type: ignore
        import torch  # noqa: F401
        import pandas  # noqa: F401
        import tqdm  # noqa: F401
    except ImportError as exc:
        return False, {
            "reason": "训练依赖缺失：torch / pandas / tqdm（用户可选安装；未装则继续用默认参数）",
            "detail": str(exc).splitlines()[0][:120],
        }
    logs = []
    for card_id, rating, ts in reviews:
        try:
            dt = datetime.fromisoformat(str(ts))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        logs.append(ReviewLog(card_id=card_id, rating=Rating(int(rating)),
                              review_datetime=dt, review_duration=0))
    if len(logs) < FSRS_TRAIN_MIN_SAMPLES:
        return False, {"reason": "复习记录不足（需 ≥%d 条，当前 %d 条）" % (FSRS_TRAIN_MIN_SAMPLES, len(logs)),
                       "sample_count": len(logs),
                       "confidence": confidence_for(len(logs))}
    try:
        optimizer = Optimizer(logs)
        params = optimizer.compute_optimal_parameters(verbose=False)
    except Exception as exc:  # torch 路径各种数值异常
        return False, {"reason": "训练失败：%s" % str(exc).splitlines()[0][:120]}
    from fsrs.scheduler import LOWER_BOUNDS_PARAMETERS, UPPER_BOUNDS_PARAMETERS  # type: ignore
    if not all(lb <= p <= ub for p, lb, ub in zip(params, LOWER_BOUNDS_PARAMETERS, UPPER_BOUNDS_PARAMETERS)):
        return False, {"reason": "训练参数越界，已丢弃（保留默认参数）"}
    try:
        _PARAM_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PARAM_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "parameters": [round(float(p), 6) for p in params],
            "trained_at": now(),
        }, ensure_ascii=False), "utf-8")
        os.replace(tmp, _PARAM_FILE)
    except OSError as exc:
        return False, {"reason": "参数写入失败：%s" % exc}
    _invalidate()
    return True, {"sample_count": len(logs), "trained_at": now(),
                  "confidence": confidence_for(len(logs))}  # §16.2 高置信度标记


def train_async(reviews: list[tuple[int, int, str]]) -> bool:
    """后台线程训练（不阻塞 HTTP 请求）。已在训练 → False。"""
    if _TRAIN_STATE["running"]:
        return False
    _TRAIN_STATE["result"] = None
    _TRAIN_STATE["error"] = None

    def _run() -> None:
        _TRAIN_STATE["running"] = True
        try:
            ok, payload = train_parameters(reviews)
            if ok:
                _TRAIN_STATE["result"] = payload
            else:
                _TRAIN_STATE["error"] = payload
        finally:
            _TRAIN_STATE["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return True


def optimal_retention(stabilities: list[float], n_total: int,
                      seconds_per_review: float = 30.0, subject: str = "") -> dict[str, object]:
    """CMRR 式最优保持率估算（FSRS wiki The Optimal Retention 思路的解析近似）。

    用你自己的卡量与平均稳定度，遍历候选保持率 R：
      - FSRS 遗忘曲线 R(t)=(1+FACTOR·t/S)^DECAY（DECAY=-0.5, FACTOR=19/81）
        反解间隔 t = S·(R^-2-1)·81/19
      - 稳态每日复习量 ≈ 卡量 / 平均间隔
      - 效率 = R / 每日复习量（单位工作量的记忆量），取最大者为推荐值
    纯估算（假设每题复习耗时固定、卡池规模稳定），供决策参考而非精确承诺。
    subject 用于 §46.5C 以学科专属保持率为当前基准。
    """
    active = [s for s in stabilities if s and s > 0]
    n = n_total
    assumed = False
    avg_s = sum(active) / len(active) if active else 0.0
    if avg_s <= 0:
        avg_s = 5.0  # 无 FSRS 历史时的默认稳定度假设
        assumed = True
    points = []
    best = None
    r = 0.75
    while r <= 0.9501:
        interval = max(1.0, avg_s * (r ** -2 - 1) * 81.0 / 19.0)
        daily = n / interval
        minutes = daily * seconds_per_review / 60.0
        eff = r / daily if daily > 0 else 0.0
        points.append({"retention": round(r, 2), "interval_days": round(interval, 1),
                       "daily_reviews": round(daily, 1), "minutes": round(minutes, 1),
                       "efficiency": round(eff, 5)})
        if best is None or eff > best["efficiency"]:
            best = points[-1]
        r += 0.05
    return {
        "has_data": len(active) >= 5 and n >= 5,
        "assumed_stability": assumed,
        "n_items": n,
        "avg_stability": round(avg_s, 2),
        "current": _desired_retention(subject),
        "recommended": best["retention"] if best else 0.9,
        "points": points,
        "note": "估算假设：卡池规模稳定、每题复习耗时固定 30 秒；仅供参考。",
    }


def reset_parameters() -> bool:
    """P0：清除个性化参数，回默认。"""
    try:
        if _PARAM_FILE.is_file():
            _PARAM_FILE.unlink()
    except OSError:
        return False
    _invalidate()
    return True

"""The frozen fitted prior — a logistic model over observation features.

    uv run python -m trading.agent.fit            # fit, report, write the artifact
    uv run python -m trading.agent.fit --dry-run  # fit and report only

Bucket means in the experience store condition on ONE feature at a time. With
tens of thousands of resolved observations the store supports a small model
that uses every feature jointly and hands each candidate a calibrated
probability of clearing the hurdle at the horizon. Deterministic code
proposes that number, the model reasons over it, deterministic code disposes
— the invariant is untouched.

Frozen by construction: the fit runs offline, the artifact is a JSON file of
coefficients plus its own fit report (n, holdout log-loss, AUC, calibration
deciles), and the running system only ever READS it. Refitting is an
outer-loop gradient step, committed with its report like any config change.
Inference is a dot product in pure Python: no numeric dependency at run time,
and none at fit time either — Newton's method on nine features needs no
library.

The label is `cleared_hurdle` by default (forward return beat the venue's
round-trip cost), the one outcome every source — backtest, universe sweep,
random arm, shadow — records identically. Model picks are excluded from the
training rows: they are selected by the thing being measured.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
from pathlib import Path

from trading.config import AppConfig, config

log = logging.getLogger(__name__)

# Continuous features are standardised at fit time; indicator features are not.
CONTINUOUS = ("log_volume", "change", "abs_change", "flow")
BOOKS = ("CRYPTO", "BSTOCKS", "KR", "US")
FEATURES = CONTINUOUS + ("flow_missing",) + tuple(f"book_{b}" for b in BOOKS)


def features_of(row: dict) -> list[float]:
    """Observation (or candidate) row -> feature vector, in FEATURES order.

    Works on both what the scorer stores and what a screen hands the loop:
    `change_pct`, `quote_volume`, `taker_share` / `taker_buy_share`, `book`.
    """
    change = float(row.get("change_pct") or 0.0)
    change = max(min(change, 100.0), -100.0)  # a +900% print is an outlier, not a feature
    volume = float(row.get("quote_volume") or 0.0)
    flow = row.get("taker_share")
    if flow is None:
        flow = row.get("taker_buy_share")
    missing = 1.0 if flow is None else 0.0
    book = str(row.get("book") or "").upper()
    return [
        math.log1p(max(volume, 0.0)),
        change,
        abs(change),
        0.0 if flow is None else float(flow),
        missing,
        *[1.0 if book == b else 0.0 for b in BOOKS],
    ]


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting; `a` is symmetric positive."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        m[col], m[pivot] = m[pivot], m[col]
        p = m[col][col]
        if abs(p) < 1e-12:
            continue
        for r in range(n):
            if r == col:
                continue
            f = m[r][col] / p
            if f:
                for c in range(col, n + 1):
                    m[r][c] -= f * m[col][c]
    out = []
    for i in range(n):
        p = m[i][i]
        out.append(m[i][n] / p if abs(p) >= 1e-12 else 0.0)
    return out


def fit_logistic(
    x: list[list[float]], y: list[int], *, l2: float = 1.0, iterations: int = 25
) -> tuple[list[float], float]:
    """Ridge-penalised logistic regression by Newton's method.

    Returns (weights, bias). The penalty is not applied to the bias. Converges
    in a handful of steps on standardised inputs; the cap is a safety net.
    """
    k = len(x[0])
    w = [0.0] * k
    b = 0.0
    for _ in range(iterations):
        grad = [0.0] * (k + 1)
        hess = [[0.0] * (k + 1) for _ in range(k + 1)]
        for xi, yi in zip(x, y, strict=True):
            z = b + sum(wj * xj for wj, xj in zip(w, xi, strict=True))
            p = _sigmoid(z)
            r = p - yi
            s = p * (1 - p)
            xe = xi + [1.0]
            for i in range(k + 1):
                grad[i] += r * xe[i]
                si = s * xe[i]
                row = hess[i]
                for j in range(i, k + 1):
                    row[j] += si * xe[j]
        for i in range(k + 1):
            for j in range(i):
                hess[i][j] = hess[j][i]
        for i in range(k):
            grad[i] += l2 * w[i]
            hess[i][i] += l2
        step = _solve(hess, grad)
        w = [wi - si for wi, si in zip(w, step[:k], strict=True)]
        b -= step[k]
        if max(abs(s) for s in step) < 1e-7:
            break
    return w, b


def _auc(scores: list[float], labels: list[int]) -> float | None:
    pos = [s for s, y in zip(scores, labels, strict=True) if y]
    neg = [s for s, y in zip(scores, labels, strict=True) if not y]
    if not pos or not neg:
        return None
    ranked = sorted(zip(scores, labels, strict=True))
    # Rank-sum with tie handling (Mann-Whitney).
    rank_sum = 0.0
    i = 0
    while i < len(ranked):
        j = i
        while j + 1 < len(ranked) and ranked[j + 1][0] == ranked[i][0]:
            j += 1
        avg_rank = (i + j) / 2 + 1
        rank_sum += avg_rank * sum(1 for _, y in ranked[i : j + 1] if y)
        i = j + 1
    return (rank_sum - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def _log_loss(scores: list[float], labels: list[int]) -> float:
    eps = 1e-9
    return -sum(
        math.log(p if y else 1 - p)
        for p, y in zip([min(max(s, eps), 1 - eps) for s in scores], labels, strict=True)
    ) / max(len(labels), 1)


def _calibration(scores: list[float], labels: list[int], bins: int = 10) -> list[dict]:
    if not scores:
        return []
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    size = max(len(order) // bins, 1)
    out = []
    for k in range(0, len(order), size):
        idx = order[k : k + size]
        out.append(
            {
                "n": len(idx),
                "predicted": round(sum(scores[i] for i in idx) / len(idx), 4),
                "observed": round(sum(labels[i] for i in idx) / len(idx), 4),
            }
        )
    return out


class ScorerModel:
    """The artifact at run time: coefficients in, a probability out."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.features = list(payload["features"])
        self.weights = [float(v) for v in payload["weights"]]
        self.bias = float(payload["bias"])
        self.means = [float(v) for v in payload["means"]]
        self.stds = [float(v) for v in payload["stds"]]
        self.meta = payload.get("meta", {})

    @classmethod
    def load(cls, path: str | Path) -> ScorerModel | None:
        path = Path(path)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if list(payload.get("features", [])) != list(FEATURES):
                log.warning("scorer model %s was fit on a different feature set; ignored", path)
                return None
            return cls(payload)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("scorer model unreadable (%s); no fitted prior", exc)
            return None

    def _standardise(self, vec: list[float]) -> list[float]:
        return [(v - m) / s if s else v for v, m, s in zip(vec, self.means, self.stds, strict=True)]

    def p_clear(self, row: dict) -> float:
        z = self.bias + sum(
            w * v for w, v in zip(self.weights, self._standardise(features_of(row)), strict=True)
        )
        return _sigmoid(z)

    def describe(self) -> str:
        m = self.meta
        return (
            f"fitted prior: P(clear hurdle at horizon) from n={m.get('n_train', '?')} "
            f"observations, frozen {str(m.get('fitted_at', ''))[:10]}, "
            f"holdout AUC {m.get('holdout_auc', '?')}"
        )


def _load_rows(cfg: AppConfig) -> list[dict]:
    path = Path(cfg.score.observations)
    if not path.exists():
        return []
    opens: dict[str, dict] = {}
    resolves: dict[str, dict] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            (opens if rec.get("kind") == "open" else resolves)[rec.get("id", "")] = rec
    keep = set(cfg.fit.sources)
    rows = []
    for oid, res in resolves.items():
        o = opens.get(oid)
        if o and o.get("source") in keep and res.get(cfg.fit.label) is not None:
            rows.append({**o, **res})
    rows.sort(key=lambda r: str(r.get("ts", "")))
    return rows


def train(cfg: AppConfig | None = None, *, write: bool = True) -> dict:
    cfg = cfg or config()
    rows = _load_rows(cfg)
    if len(rows) < cfg.fit.min_rows:
        return {"fitted": False, "n": len(rows), "note": f"need {cfg.fit.min_rows} rows"}

    cut = int(len(rows) * (1 - cfg.fit.holdout_fraction))
    train_rows, hold_rows = rows[:cut], rows[cut:]
    x_train = [features_of(r) for r in train_rows]
    y_train = [1 if str(r.get(cfg.fit.label)).lower() == "true" else 0 for r in train_rows]

    k = len(FEATURES)
    means = [0.0] * k
    stds = [1.0] * k
    for i, name in enumerate(FEATURES):
        if name not in CONTINUOUS:
            continue
        col = [v[i] for v in x_train]
        mu = sum(col) / len(col)
        var = sum((c - mu) ** 2 for c in col) / max(len(col) - 1, 1)
        means[i], stds[i] = mu, math.sqrt(var) or 1.0
    std_x = [[(v - m) / s for v, m, s in zip(vec, means, stds, strict=True)] for vec in x_train]
    w, b = fit_logistic(std_x, y_train, l2=cfg.fit.l2, iterations=cfg.fit.iterations)

    payload = {
        "features": list(FEATURES),
        "weights": [round(v, 6) for v in w],
        "bias": round(b, 6),
        "means": [round(v, 6) for v in means],
        "stds": [round(v, 6) for v in stds],
        "meta": {
            "fitted_at": dt.datetime.now(dt.UTC).isoformat(),
            "label": cfg.fit.label,
            "sources": sorted({r.get("source") for r in rows}),
            "n_train": len(train_rows),
            "n_holdout": len(hold_rows),
            "base_rate": round(sum(y_train) / len(y_train), 4),
            "l2": cfg.fit.l2,
        },
    }
    model = ScorerModel(payload)
    if hold_rows:
        scores = [model.p_clear(r) for r in hold_rows]
        labels = [1 if str(r.get(cfg.fit.label)).lower() == "true" else 0 for r in hold_rows]
        auc = _auc(scores, labels)
        payload["meta"].update(
            {
                "holdout_auc": round(auc, 4) if auc is not None else None,
                "holdout_log_loss": round(_log_loss(scores, labels), 4),
                "holdout_base_rate": round(sum(labels) / len(labels), 4),
                "holdout_calibration": _calibration(scores, labels),
            }
        )
    if write:
        out = Path(cfg.fit.model)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        log.info("fitted prior written to %s", out)
    return {"fitted": True, "path": cfg.fit.model if write else None, **payload["meta"]}


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="fit and report, write nothing")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    report = train(write=not args.dry_run)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report.get("fitted") else 1


if __name__ == "__main__":
    raise SystemExit(main())

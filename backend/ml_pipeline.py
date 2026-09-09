"""Auditable ML research pipeline for chronological Indian-equity experiments.

This module deliberately keeps model prediction separate from order execution.
It trains a small regularised logistic model with no third-party dependency, so
the complete training behaviour and serialized version are inspectable.
"""
import base64
import csv
import hashlib
import hmac
import json
import math
import os
import pickle
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, pstdev
from typing import Dict, Iterable, List, Optional, Tuple

from .config import (DATABASE_URL, IS_PRODUCTION, ML_MAX_SYMBOLS_PER_SESSION, ML_MIN_ADTV,
                     ML_MIN_COVERAGE_PCT, ML_MIN_DAILY_BARS, ML_MIN_TRAIN_SAMPLES,
                     ML_RESEARCH_PATH, ML_TRAINING_EXCHANGES, MODEL_ARTIFACT_KEY)
from .database import now_iso
from .history_store import HistoryStore
from .ml.calibration import apply_calibration, fit_isotonic
from .ml.trading_policy import policy_manifest, rank_candidates, score_candidate


FEATURES_V1 = ("return_1d","return_5d","sma20_gap","sma50_gap","volatility_20d","rsi_14","atr_14","volume_z20","range_pct","trend_regime")
FEATURES_V2 = FEATURES_V1 + ("return_20d","relative_strength_20d","market_return_1d","market_return_20d",
                            "market_breadth","market_volatility_20d","gap_pct","close_location",
                            "volume_trend","downside_volatility_20d","atr_regime")
FEATURE_SETS = {"daily_v1": FEATURES_V1, "daily_v2": FEATURES_V2}
DEFAULT_FEATURE_SET = "daily_v2"
FEATURES = FEATURES_V2
_ESTIMATOR_CACHE = {}
os.environ.setdefault("LOKY_MAX_CPU_COUNT","1")


def _decode_json(value):
    return value if isinstance(value,(dict,list)) else json.loads(value)
SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS corporate_actions(
 exchange TEXT NOT NULL,symbol TEXT NOT NULL,effective_date TEXT NOT NULL,
 action_type TEXT NOT NULL CHECK(action_type IN ('split','bonus','dividend')),
 ratio_from REAL NOT NULL DEFAULT 1,ratio_to REAL NOT NULL DEFAULT 1,
 cash_amount REAL NOT NULL DEFAULT 0,source TEXT NOT NULL,created_at TEXT NOT NULL,
 PRIMARY KEY(exchange,symbol,effective_date,action_type)
);
CREATE TABLE IF NOT EXISTS corporate_action_ingestions(
 exchange TEXT NOT NULL,period_start TEXT NOT NULL,period_end TEXT NOT NULL,url TEXT NOT NULL,
 sha256 TEXT NOT NULL,row_count INTEGER NOT NULL,status TEXT NOT NULL,imported_at TEXT NOT NULL,
 PRIMARY KEY(exchange,period_start,period_end)
);
CREATE TABLE IF NOT EXISTS universe_membership(
 exchange TEXT NOT NULL,symbol TEXT NOT NULL,valid_from TEXT NOT NULL,valid_to TEXT,
 status TEXT NOT NULL,source TEXT NOT NULL,created_at TEXT NOT NULL,
 PRIMARY KEY(exchange,symbol,valid_from)
);
CREATE TABLE IF NOT EXISTS feature_rows(
 exchange TEXT NOT NULL,symbol TEXT NOT NULL,timestamp TEXT NOT NULL,
 feature_set TEXT NOT NULL,features TEXT NOT NULL,label INTEGER,label_return REAL,
 source_hash TEXT NOT NULL,created_at TEXT NOT NULL,
 PRIMARY KEY(exchange,symbol,timestamp,feature_set)
);
CREATE INDEX IF NOT EXISTS idx_features_set_time ON feature_rows(feature_set,timestamp);
CREATE TABLE IF NOT EXISTS model_versions(
 id INTEGER PRIMARY KEY AUTOINCREMENT,model_name TEXT NOT NULL,version TEXT UNIQUE NOT NULL,
 feature_set TEXT NOT NULL,algorithm TEXT NOT NULL,payload TEXT NOT NULL,
 training_start TEXT,training_end TEXT,training_samples INTEGER NOT NULL,
 metrics TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS validation_runs(
 id INTEGER PRIMARY KEY AUTOINCREMENT,model_version TEXT NOT NULL,validation_type TEXT NOT NULL,
 period_start TEXT,period_end TEXT,symbols INTEGER NOT NULL,trades INTEGER NOT NULL,
 metrics TEXT NOT NULL,created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shadow_predictions(
 id INTEGER PRIMARY KEY AUTOINCREMENT,model_version TEXT NOT NULL,exchange TEXT NOT NULL,
 symbol TEXT NOT NULL,timestamp TEXT NOT NULL,probability REAL NOT NULL,signal INTEGER NOT NULL,
 realised_return REAL,status TEXT NOT NULL,created_at TEXT NOT NULL,
 UNIQUE(model_version,exchange,symbol,timestamp)
);
CREATE TABLE IF NOT EXISTS drift_reports(
 id INTEGER PRIMARY KEY AUTOINCREMENT,model_version TEXT NOT NULL,feature TEXT NOT NULL,
 reference_mean REAL NOT NULL,current_mean REAL NOT NULL,standardised_shift REAL NOT NULL,
 status TEXT NOT NULL,created_at TEXT NOT NULL
);
"""


def _safe_std(values: List[float]) -> float:
    if len(values)>1:
        average=sum(values)/len(values); value=math.sqrt(sum((item-average)**2 for item in values)/len(values))
    else: value=0.0
    return value if value > 1e-9 else 1.0


def _raw_std(values: List[float]) -> float:
    if len(values)<2: return 0.0
    average=sum(values)/len(values)
    return math.sqrt(sum((item-average)**2 for item in values)/len(values))


def _rsi(values: List[float], period: int = 14) -> float:
    changes=[b-a for a,b in zip(values[-period-1:-1],values[-period:])]
    gains=mean([max(0,x) for x in changes]) if changes else 0
    losses=mean([max(0,-x) for x in changes]) if changes else 0
    return 100.0 if losses == 0 else 100 - 100/(1+gains/losses)


class ResearchStore:
    def __new__(cls,path: Path = ML_RESEARCH_PATH):
        if cls is ResearchStore and DATABASE_URL and Path(path)==Path(ML_RESEARCH_PATH):
            from .postgres_repositories import PostgresResearchStore
            return PostgresResearchStore()
        return super().__new__(cls)

    def __init__(self, path: Path = ML_RESEARCH_PATH):
        self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db: db.executescript(SCHEMA)

    def connect(self):
        db=sqlite3.connect(self.path,timeout=30); db.row_factory=sqlite3.Row; return db

    def add_action(self, exchange: str, symbol: str, effective_date: str, action_type: str,
                   ratio_from: float = 1, ratio_to: float = 1, cash_amount: float = 0, source: str = "manual"):
        if action_type not in {"split","bonus","dividend"}: raise ValueError("Unsupported corporate action")
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO corporate_actions VALUES(?,?,?,?,?,?,?,?,?)", (exchange.upper(),symbol.upper(),effective_date,action_type,ratio_from,ratio_to,cash_amount,source,now_iso()))

    def actions(self, exchange: str, symbol: str) -> List[Dict]:
        with self.connect() as db:
            return [dict(x) for x in db.execute("SELECT * FROM corporate_actions WHERE exchange=? AND symbol=? ORDER BY effective_date",(exchange,symbol)).fetchall()]

    def status(self) -> Dict:
        with self.connect() as db:
            counts={table:db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("corporate_actions","corporate_action_ingestions","universe_membership","feature_rows","model_versions","validation_runs","shadow_predictions","drift_reports")}
            latest=db.execute("SELECT version,status,metrics,created_at FROM model_versions WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()
            experiment=db.execute("SELECT version,status,metrics,created_at FROM model_versions ORDER BY id DESC LIMIT 1").fetchone()
        return {"counts":counts,"latest_model":({**dict(latest),"metrics":_decode_json(latest["metrics"])} if latest else None),
                "latest_experiment":({**dict(experiment),"metrics":_decode_json(experiment["metrics"])} if experiment else None)}


def adjust_corporate_actions(bars: List[Dict], actions: List[Dict]) -> List[Dict]:
    """Back-adjust OHLC/volume. Dividend adjustment is an auditable approximation."""
    adjusted=[dict(x) for x in bars]
    for action in actions:
        date=action["effective_date"]
        if action["action_type"] in {"split","bonus"}:
            factor=float(action["ratio_from"])/max(1e-9,float(action["ratio_to"]))
            volume_factor=1/max(1e-9,factor)
        else:
            event=next((x for x in adjusted if x["timestamp"][:10]>=date),None)
            reference=float(event["close"]) if event else 0
            factor=max(.01,(reference-float(action["cash_amount"]))/reference) if reference else 1
            volume_factor=1
        for row in adjusted:
            if row["timestamp"][:10] < date:
                for key in ("open","high","low","close"): row[key]=round(float(row[key])*factor,6)
                row["volume"]=int(float(row.get("volume",0))*volume_factor)
    return adjusted


def _triple_barrier(bars: List[Dict], index: int, atr_ratio: float, horizon: int = 10,
                    barrier_multiple: float = 1.5) -> Tuple[Optional[int], Optional[float]]:
    close=float(bars[index]["close"]); distance=max(close*atr_ratio*barrier_multiple,close*.005)
    upper=close+distance; lower=max(.01,close-distance)
    for future in bars[index+1:min(len(bars),index+horizon+1)]:
        hit_upper=float(future["high"])>=upper; hit_lower=float(future["low"])<=lower
        if hit_upper and hit_lower: return None,None  # daily OHLC cannot identify which hit first
        if hit_upper: return 1,upper/close-1
        if hit_lower: return 0,lower/close-1
    return None,None


def feature_rows(exchange: str, symbol: str, bars: List[Dict], horizon: int = 10,
                 market_context: Optional[Dict[str,Dict]] = None,
                 feature_set: str = DEFAULT_FEATURE_SET) -> List[Dict]:
    if feature_set not in FEATURE_SETS: raise ValueError(f"Unknown feature set {feature_set}")
    market_context=market_context or {}
    closes=[float(x["close"]) for x in bars]; volumes=[float(x.get("volume",0)) for x in bars]
    true_ranges=[0.0]
    for j in range(1,len(bars)):
        prev=closes[j-1]; true_ranges.append(max(float(bars[j]["high"])-float(bars[j]["low"]),abs(float(bars[j]["high"])-prev),abs(float(bars[j]["low"])-prev))/max(prev,1e-9))
    rows=[]
    for i in range(50,len(bars)):
        c=closes[i]; window20=closes[i-19:i+1]; window50=closes[i-49:i+1]
        returns=[closes[j]/closes[j-1]-1 for j in range(i-19,i+1)]
        trs=true_ranges[i-13:i+1]; atr=sum(trs)/len(trs); mean20=sum(window20)/len(window20); mean50=sum(window50)/len(window50)
        vwin=volumes[i-19:i+1]; future=closes[i+horizon]/c-1 if i+horizon<len(bars) else None
        mean_volume=sum(vwin)/len(vwin)
        current_trs=true_ranges[max(1,i-59):i+1]
        context=market_context.get(bars[i]["timestamp"],{})
        values={
            "return_1d":closes[i]/closes[i-1]-1,"return_5d":closes[i]/closes[i-5]-1,
            "sma20_gap":c/mean20-1,"sma50_gap":c/mean50-1,
            "volatility_20d":_raw_std(returns),"rsi_14":(_rsi(closes[max(0,i-14):i+1])-50)/50,
            "atr_14":atr,"volume_z20":(volumes[i]-mean_volume)/_safe_std(vwin),
            "range_pct":(float(bars[i]["high"])-float(bars[i]["low"]))/max(c,1e-9),
            "trend_regime":1.0 if mean20>mean50 else -1.0,
            "average_daily_value_20d":sum(volumes[j]*closes[j] for j in range(i-19,i+1))/20,
        }
        if feature_set == "daily_v2":
            downside=[min(0,value) for value in returns]
            values.update({
                "return_20d":closes[i]/closes[i-20]-1,
                "relative_strength_20d":closes[i]/closes[i-20]-1-float(context.get("market_return_20d",0)),
                "market_return_1d":float(context.get("market_return_1d",0)),
                "market_return_20d":float(context.get("market_return_20d",0)),
                "market_breadth":float(context.get("market_breadth",.5))-.5,
                "market_volatility_20d":float(context.get("market_volatility_20d",0)),
                "gap_pct":float(bars[i]["open"])/max(closes[i-1],1e-9)-1,
                "close_location":(c-float(bars[i]["low"]))/max(float(bars[i]["high"])-float(bars[i]["low"]),1e-9)-.5,
                "volume_trend":(sum(volumes[i-4:i+1])/5)/max(mean_volume,1)-1,
                "downside_volatility_20d":_raw_std(downside),
                "atr_regime":sum(value<=atr for value in current_trs)/max(1,len(current_trs))-.5,
            })
            label,future=_triple_barrier(bars,i,atr,horizon)
        else:
            label=1 if future is not None and future>.003 else 0 if future is not None and future<-.003 else None
        digest=hashlib.sha256(json.dumps({"bar":bars[i],"features":values},sort_keys=True).encode()).hexdigest()
        rows.append({"exchange":exchange,"symbol":symbol,"timestamp":bars[i]["timestamp"],"features":values,"label":label,"label_return":future,"source_hash":digest})
    return rows


def persist_features(store: ResearchStore, rows: Iterable[Dict], feature_set: str = DEFAULT_FEATURE_SET) -> int:
    values=list(rows)
    with store.connect() as db:
        db.executemany("""INSERT INTO feature_rows(exchange,symbol,timestamp,feature_set,features,label,label_return,source_hash,created_at)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(exchange,symbol,timestamp,feature_set) DO UPDATE SET features=EXCLUDED.features,label=EXCLUDED.label,
            label_return=EXCLUDED.label_return,source_hash=EXCLUDED.source_hash,created_at=EXCLUDED.created_at""",
            [(x["exchange"],x["symbol"],x["timestamp"],feature_set,json.dumps(x["features"],sort_keys=True),x["label"],x["label_return"],x["source_hash"],now_iso()) for x in values])
    return len(values)


def build_research_dataset(history: Optional[HistoryStore] = None, store: Optional[ResearchStore] = None,
                           feature_set: str = DEFAULT_FEATURE_SET) -> Dict:
    history=history or HistoryStore(); store=store or ResearchStore(); total=0; symbols=[]; pending=[]
    context=history.market_context("NSE","day") if feature_set == "daily_v2" else {}
    eligible=history.liquid_symbol_keys(ML_MIN_ADTV,20,"day") if feature_set == "daily_v2" else None
    for series in history.series("day",80,eligible):
        bars=adjust_corporate_actions(series["bars"],store.actions(series["exchange"],series["symbol"]))
        rows=feature_rows(series["exchange"],series["symbol"],bars,market_context=context,feature_set=feature_set)
        pending.extend(rows); symbols.append(f"{series['exchange']}:{series['symbol']}")
        if len(pending)>=100000:
            total+=persist_features(store,pending,feature_set); pending=[]
    if pending: total+=persist_features(store,pending,feature_set)
    return {"symbols":symbols,"symbols_count":len(symbols),"samples":total,"feature_set":feature_set,
            "liquidity_floor":ML_MIN_ADTV if eligible is not None else None,
            "minimum_daily_bars":ML_MIN_DAILY_BARS if eligible is not None else None,
            "minimum_coverage_pct":ML_MIN_COVERAGE_PCT if eligible is not None else None,
            "training_exchanges":sorted(ML_TRAINING_EXCHANGES),"generated_at":now_iso()}


def prune_research_dataset(history: Optional[HistoryStore] = None, store: Optional[ResearchStore] = None) -> Dict:
    """Remove derived features whose underlying equity series no longer exists."""
    history=history or HistoryStore(); store=store or ResearchStore(); keys=history.symbol_keys("day",80)
    with store.connect() as db:
        before=db.execute("SELECT COUNT(*) FROM feature_rows").fetchone()[0]
        db.execute("CREATE TEMP TABLE valid_feature_symbols(exchange TEXT,symbol TEXT,PRIMARY KEY(exchange,symbol))")
        db.executemany("INSERT INTO valid_feature_symbols VALUES(?,?)",keys)
        db.execute("DELETE FROM feature_rows WHERE NOT EXISTS (SELECT 1 FROM valid_feature_symbols v WHERE v.exchange=feature_rows.exchange AND v.symbol=feature_rows.symbol)")
        after=db.execute("SELECT COUNT(*) FROM feature_rows").fetchone()[0]
    return {"removed":before-after,"retained":after,"symbols":len(keys),"feature_set":"daily_v1"}


def import_corporate_actions(path: Path, store: Optional[ResearchStore] = None) -> int:
    """Import authoritative CSV: exchange,symbol,effective_date,action_type,ratio_from,ratio_to,cash_amount,source."""
    store=store or ResearchStore(); count=0
    with Path(path).open(newline="",encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            store.add_action(row["exchange"],row["symbol"],row["effective_date"],row["action_type"],float(row.get("ratio_from") or 1),float(row.get("ratio_to") or 1),float(row.get("cash_amount") or 0),row.get("source") or "imported")
            count+=1
    return count


def import_universe_membership(path: Path, store: Optional[ResearchStore] = None) -> int:
    """Import point-in-time CSV including delisted securities and membership dates."""
    store=store or ResearchStore(); rows=[]
    with Path(path).open(newline="",encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            rows.append((row["exchange"].upper(),row["symbol"].upper(),row["valid_from"],row.get("valid_to") or None,row.get("status") or "active",row.get("source") or "imported",now_iso()))
    with store.connect() as db: db.executemany("""INSERT INTO universe_membership(exchange,symbol,valid_from,valid_to,status,source,created_at) VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(exchange,symbol,valid_from) DO UPDATE SET valid_to=EXCLUDED.valid_to,status=EXCLUDED.status,source=EXCLUDED.source,created_at=EXCLUDED.created_at""",rows)
    return len(rows)


def _sigmoid(x: float) -> float:
    if x>=0: return 1/(1+math.exp(-min(40,x)))
    exp=math.exp(max(-40,x)); return exp/(1+exp)


def _reference(samples: List[Dict], features: Tuple[str,...]) -> Dict:
    try:
        import numpy as np
        matrix=np.asarray([[float(row["features"][feature]) for feature in features] for row in samples],dtype=np.float64)
        means=matrix.mean(axis=0); stds=matrix.std(axis=0)
        return {feature:{"mean":float(means[index]),"std":float(stds[index] if stds[index]>1e-9 else 1.0)} for index,feature in enumerate(features)}
    except ImportError:
        return {feature:{"mean":mean([float(row["features"][feature]) for row in samples]),
                         "std":_safe_std([float(row["features"][feature]) for row in samples])}
                for feature in features}


def _fit(samples: List[Dict], epochs: int = 120, learning_rate: float = .08, l2: float = .01,
         features: Tuple[str,...] = FEATURES_V2) -> Dict:
    matrix=[[float(x["features"][f]) for f in features] for x in samples]; labels=[int(x["label"]) for x in samples]
    weights=[0.0]*len(features); bias=0.0; n=len(matrix)
    try:
        import numpy as np
        raw=np.asarray(matrix,dtype=np.float64); y=np.asarray(labels,dtype=np.float64)
        means_array=raw.mean(axis=0); stds_array=raw.std(axis=0); stds_array[stds_array<=1e-9]=1.0
        x=np.clip((raw-means_array)/stds_array,-10,10); means=means_array.tolist(); stds=stds_array.tolist()
        w=np.zeros(len(features),dtype=np.float64); b=0.0
        for _ in range(epochs):
            # Elementwise reductions avoid platform BLAS warnings observed on
            # some NumPy/macOS builds for tall-skinny matrix multiplication.
            logits=np.clip((x*w).sum(axis=1)+b,-40,40); errors=1/(1+np.exp(-logits))-y
            b-=learning_rate*float(errors.mean())
            w-=learning_rate*((x*errors[:,None]).mean(axis=0)+l2*w)
        weights=w.tolist(); bias=float(b)
    except ImportError:
        means=[mean([row[j] for row in matrix]) for j in range(len(features))]
        stds=[_safe_std([row[j] for row in matrix]) for j in range(len(features))]
        xs=[[max(-10.0,min(10.0,(row[j]-means[j])/stds[j])) for j in range(len(features))] for row in matrix]
        for _ in range(epochs):
            gw=[0.0]*len(weights); gb=0.0
            for row,label in zip(xs,labels):
                err=_sigmoid(bias+sum(w*v for w,v in zip(weights,row)))-label; gb+=err
                for j,value in enumerate(row): gw[j]+=err*value
            bias-=learning_rate*gb/n
            for j in range(len(weights)): weights[j]-=learning_rate*(gw[j]/n+l2*weights[j])
    return {"algorithm":"regularised_logistic_regression","features":list(features),"means":means,"stds":stds,"weights":weights,"bias":bias,"epochs":epochs,"learning_rate":learning_rate,"l2":l2}


def _fit_hgb(samples: List[Dict], features: Tuple[str,...], variant: str = "conservative") -> Dict:
    import numpy as np
    from sklearn.ensemble import HistGradientBoostingClassifier
    x=np.asarray([[float(row["features"][feature]) for feature in features] for row in samples],dtype=np.float64)
    y=np.asarray([int(row["label"]) for row in samples],dtype=np.int8)
    params={"loss":"log_loss","random_state":42,"early_stopping":False,"class_weight":"balanced"}
    if variant == "conservative":
        params.update({"learning_rate":.05,"max_iter":140,"max_leaf_nodes":15,"min_samples_leaf":80,"l2_regularization":3.0,"max_features":.8})
    else:
        params.update({"learning_rate":.04,"max_iter":180,"max_leaf_nodes":31,"min_samples_leaf":50,"l2_regularization":5.0,"max_features":.7})
    estimator=HistGradientBoostingClassifier(**params).fit(x,y)
    encoded=base64.b64encode(pickle.dumps(estimator,protocol=pickle.HIGHEST_PROTOCOL)).decode("ascii")
    signature=hmac.new(MODEL_ARTIFACT_KEY.encode(),encoded.encode(),hashlib.sha256).hexdigest()
    return {"algorithm":f"hist_gradient_boosting_{variant}","features":list(features),"estimator_b64":encoded,"estimator_hmac":signature,
            "params":params,"iterations":int(estimator.n_iter_)}


def _fit_mlp(samples: List[Dict], features: Tuple[str,...], fast: bool = False) -> Dict:
    """Train a compact DNN/MLP classifier for nonlinear pattern detection.

    The network is deliberately small and regularised.  In this project it is a
    research candidate only: promotion still requires the same untouched
    holdout and walk-forward comparison as every other model.
    """
    import numpy as np
    from sklearn.neural_network import MLPClassifier
    from sklearn.preprocessing import StandardScaler
    from sklearn.pipeline import Pipeline
    x=np.asarray([[float(row["features"][feature]) for feature in features] for row in samples],dtype=np.float64)
    y=np.asarray([int(row["label"]) for row in samples],dtype=np.int8)
    estimator=Pipeline([
        ("scale", StandardScaler()),
        ("mlp", MLPClassifier(
            hidden_layer_sizes=(64,32),
            activation="relu",
            solver="adam",
            alpha=0.003,
            batch_size=256,
            learning_rate_init=0.001,
            max_iter=80 if fast else 180,
            early_stopping=True,
            validation_fraction=0.15,
            n_iter_no_change=12,
            random_state=42,
        )),
    ]).fit(x,y)
    encoded=base64.b64encode(pickle.dumps(estimator,protocol=pickle.HIGHEST_PROTOCOL)).decode("ascii")
    signature=hmac.new(MODEL_ARTIFACT_KEY.encode(),encoded.encode(),hashlib.sha256).hexdigest()
    return {"algorithm":"dnn_mlp_conservative","features":list(features),"estimator_b64":encoded,
            "estimator_hmac":signature,"architecture":{"hidden_layers":[64,32],"activation":"relu",
            "regularisation_alpha":0.003,"early_stopping":True}}


def _fit_soft_voting_ensemble(samples: List[Dict], features: Tuple[str,...], fast: bool = False) -> Dict:
    """Blend linear, gradient-boosted and DNN classifiers by probability.

    This gives the model-selection process a modern nonlinear ensemble while
    preserving per-component signatures and an auditable average prediction.
    """
    members=[
        _fit(samples,epochs=40 if fast else 120,features=features),
        _fit_hgb(samples,features,"conservative"),
        _fit_hgb(samples,features,"flexible"),
        _fit_mlp(samples,features,fast=fast),
    ]
    return {"algorithm":"soft_voting_logistic_hgb_dnn_ensemble","features":list(features),
            "ensemble_models":members,"weights":[0.20,0.30,0.30,0.20],
            "objective":"reduce single-model overfit by averaging signed model probabilities"}


def _fit_algorithm(samples: List[Dict], algorithm: str, features: Tuple[str,...], fast: bool = False) -> Dict:
    if algorithm == "regularised_logistic_regression": return _fit(samples,epochs=40 if fast else 120,features=features)
    if algorithm == "dnn_mlp_conservative": return _fit_mlp(samples,features,fast=fast)
    if algorithm == "soft_voting_logistic_hgb_dnn_ensemble": return _fit_soft_voting_ensemble(samples,features,fast=fast)
    variant="flexible" if algorithm.endswith("flexible") else "conservative"
    return _fit_hgb(samples,features,variant)


def _raw_predict(model: Dict, features: Dict) -> float:
    if model.get("ensemble_models"):
        weights=[float(x) for x in model.get("weights") or [1]*len(model["ensemble_models"])]
        total=max(1e-9,sum(weights))
        return float(sum(weight*_raw_predict(member,features) for weight,member in zip(weights,model["ensemble_models"]))/total)
    if model.get("estimator_b64"):
        import numpy as np
        encoded=model["estimator_b64"]; key=hashlib.sha256(encoded.encode()).hexdigest()
        signature=model.get("estimator_hmac","")
        expected=hmac.new(MODEL_ARTIFACT_KEY.encode(),encoded.encode(),hashlib.sha256).hexdigest()
        if not signature:
            if IS_PRODUCTION: raise ValueError("Unsigned legacy model artifact is forbidden in production")
        elif not hmac.compare_digest(signature,expected):
            raise ValueError("Model artifact integrity verification failed")
        estimator=_ESTIMATOR_CACHE.get(key)
        if estimator is None:
            estimator=pickle.loads(base64.b64decode(encoded)); _ESTIMATOR_CACHE[key]=estimator
        row=np.asarray([[float(features[f]) for f in model["features"]]],dtype=np.float64)
        return float(estimator.predict_proba(row)[0,1])
    row=[(float(features[f])-model["means"][i])/model["stds"][i] for i,f in enumerate(model["features"])]
    return _sigmoid(model["bias"]+sum(w*v for w,v in zip(model["weights"],row)))


def predict(model: Dict, features: Dict) -> float:
    return apply_calibration(_raw_predict(model, features), model.get("calibration", {}))


def _with_isotonic_calibration(model: Dict, calibration_samples: List[Dict]) -> Dict:
    probabilities=[_raw_predict(model,row["features"]) for row in calibration_samples]
    calibrated=dict(model)
    try:
        calibrated["calibration"]=fit_isotonic(probabilities,[int(row["label"]) for row in calibration_samples])
    except ValueError as exc:
        calibrated["calibration"]={"method":"identity","samples":len(calibration_samples),"reason":str(exc)}
    return calibrated


def _fit_chronologically_calibrated(samples: List[Dict], algorithm: str, features: Tuple[str,...],
                                     purge: int, fast: bool = False) -> Dict:
    dates=sorted(set(row["timestamp"] for row in samples))
    split=max(1,int(len(dates)*.80))
    base_dates=set(dates[:max(1,split-purge)])
    calibration_dates=set(dates[split:])
    base=[row for row in samples if row["timestamp"] in base_dates]
    calibration=[row for row in samples if row["timestamp"] in calibration_dates]
    if not base or len(calibration)<50:
        return _fit_algorithm(samples,algorithm,features,fast)
    return _with_isotonic_calibration(_fit_algorithm(base,algorithm,features,fast),calibration)


def _classification(model: Dict, samples: List[Dict]) -> Dict:
    probs=[predict(model,x["features"]) for x in samples]; predictions=[int(p>=.5) for p in probs]
    labels=[x["label"] for x in samples]; correct=sum(a==b for a,b in zip(predictions,labels))
    tp=sum(p==1 and y==1 for p,y in zip(predictions,labels)); fp=sum(p==1 and y==0 for p,y in zip(predictions,labels)); fn=sum(p==0 and y==1 for p,y in zip(predictions,labels))
    logloss=-mean([y*math.log(max(1e-9,p))+(1-y)*math.log(max(1e-9,1-p)) for p,y in zip(probs,labels)])
    brier=mean([(p-y)**2 for p,y in zip(probs,labels)])
    return {"samples":len(samples),"accuracy":round(correct/max(1,len(samples))*100,2),"precision":round(tp/max(1,tp+fp)*100,2),"recall":round(tp/max(1,tp+fn)*100,2),"log_loss":round(logloss,4),"brier_score":round(brier,4)}


def _load_samples(store: ResearchStore, feature_set: str = DEFAULT_FEATURE_SET) -> List[Dict]:
    liquidity="CAST(features->>'average_daily_value_20d' AS DOUBLE PRECISION)" if getattr(store,"is_postgres",False) else "CAST(json_extract(features,'$.average_daily_value_20d') AS REAL)"
    exchanges=sorted(ML_TRAINING_EXCHANGES or {"NSE"})
    exchange_placeholders=",".join(["?"]*len(exchanges))
    with store.connect() as db:
        rows=db.execute(f"""
          WITH deduplicated AS (
            SELECT exchange,symbol,timestamp,features,label,label_return,
              ROW_NUMBER() OVER (
                PARTITION BY timestamp,symbol
                ORDER BY CASE WHEN exchange='NSE' THEN 0 ELSE 1 END
              ) venue_rank
            FROM feature_rows
            WHERE feature_set=? AND label IS NOT NULL
              AND exchange IN ({exchange_placeholders})
              AND {liquidity}>=?
          ), eligible AS (
            SELECT exchange,symbol,timestamp,features,label,label_return,
              ROW_NUMBER() OVER (
                PARTITION BY timestamp
                ORDER BY {liquidity} DESC,exchange,symbol
              ) liquidity_rank
            FROM deduplicated WHERE venue_rank=1
          )
          SELECT exchange,symbol,timestamp,features,label,label_return FROM eligible
          WHERE liquidity_rank<=? ORDER BY timestamp,exchange,symbol
        """,(feature_set,*exchanges,ML_MIN_ADTV,max(1,ML_MAX_SYMBOLS_PER_SESSION))).fetchall()
    return [{**dict(x),"features":_decode_json(x["features"])} for x in rows]


def train_model(store: Optional[ResearchStore] = None, feature_set: str = DEFAULT_FEATURE_SET) -> Dict:
    store=store or ResearchStore(); samples=_load_samples(store,feature_set)
    if len(samples)<ML_MIN_TRAIN_SAMPLES: raise ValueError(f"Need at least {ML_MIN_TRAIN_SAMPLES} labelled samples; only {len(samples)} available")
    features=FEATURE_SETS[feature_set]; dates=sorted(set(x["timestamp"] for x in samples)); purge=10 if feature_set=="daily_v2" else 5
    calibration_start=max(1,int(len(dates)*.60)); selection_start=max(calibration_start+1,int(len(dates)*.70)); final_start=max(selection_start+1,int(len(dates)*.80))
    development_dates=set(dates[:max(1,calibration_start-purge)])
    calibration_dates=set(dates[calibration_start:max(calibration_start,selection_start-purge)])
    selection_dates=set(dates[selection_start:max(selection_start,final_start-purge)])
    development=[x for x in samples if x["timestamp"] in development_dates]
    calibration=[x for x in samples if x["timestamp"] in calibration_dates]
    selection=[x for x in samples if x["timestamp"] in selection_dates]
    final=[x for x in samples if x["timestamp"] in set(dates[final_start:])]
    if not development or not calibration or not selection or not final: raise ValueError("Need more chronological dates for development, calibration, selection and untouched final blocks")
    candidates=["regularised_logistic_regression","hist_gradient_boosting_conservative","hist_gradient_boosting_flexible",
                "dnn_mlp_conservative","soft_voting_logistic_hgb_dnn_ensemble"]
    comparison=[]
    for algorithm in candidates:
        uncalibrated=_fit_algorithm(development,algorithm,features)
        raw_score=_classification(uncalibrated,selection)
        candidate=_with_isotonic_calibration(uncalibrated,calibration)
        score=_classification(candidate,selection)
        comparison.append({"algorithm":algorithm,"calibration":"isotonic","selection":score,"uncalibrated_selection":raw_score})
    winner=min(comparison,key=lambda item:(item["selection"]["log_loss"],item["selection"]["brier_score"]))["algorithm"]
    final_calibration_start=selection_start
    refit_dates=set(dates[:max(1,final_calibration_start-purge)]); refit=[x for x in samples if x["timestamp"] in refit_dates]
    final_calibration_dates=set(dates[final_calibration_start:max(final_calibration_start,final_start-purge)])
    final_calibration=[x for x in samples if x["timestamp"] in final_calibration_dates]
    model=_with_isotonic_calibration(_fit_algorithm(refit,winner,features),final_calibration)
    metrics={"train":_classification(model,refit),"calibration":_classification(model,final_calibration),"holdout":_classification(model,final),
             "selection_comparison":comparison,"untouched_final":True,"purge_sessions":purge,
             "development_period":[dates[0],dates[calibration_start-1]],"calibration_period":[dates[calibration_start],dates[selection_start-1]],"selection_period":[dates[selection_start],dates[final_start-1]],"final_period":[dates[final_start],dates[-1]]}
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"); version=f"direction-v2.6-{stamp}"
    payload={**model,"reference":_reference(refit,features),"decision_threshold":.55,"live_eligible":False,
             "selection_comparison":comparison,"untouched_final":True,"trading_policy":policy_manifest()}
    with store.connect() as db:
        db.execute("UPDATE model_versions SET status='rejected' WHERE status='candidate'")
        db.execute("INSERT INTO model_versions(model_name,version,feature_set,algorithm,payload,training_start,training_end,training_samples,metrics,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",("direction_classifier",version,feature_set,winner,json.dumps(payload),refit[0]["timestamp"],refit[-1]["timestamp"],len(refit),json.dumps(metrics),"candidate",now_iso()))
    return {"version":version,"algorithm":winner,"metrics":metrics,"training_samples":len(refit),"holdout_samples":len(final),"status":"candidate","live_eligible":False}


def active_model(store: Optional[ResearchStore] = None) -> Optional[Dict]:
    store=store or ResearchStore()
    with store.connect() as db: row=db.execute("SELECT * FROM model_versions WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()
    return {**dict(row),"payload":_decode_json(row["payload"]),"metrics":_decode_json(row["metrics"])} if row else None


def validation_model(store: Optional[ResearchStore] = None) -> Optional[Dict]:
    """Return an unvalidated candidate without displacing the paper baseline."""
    store=store or ResearchStore()
    with store.connect() as db:
        row=db.execute("SELECT * FROM model_versions WHERE status='candidate' ORDER BY id DESC LIMIT 1").fetchone()
    if row: return {**dict(row),"payload":_decode_json(row["payload"]),"metrics":_decode_json(row["metrics"])}
    return active_model(store)


def _load_all_features(store: ResearchStore, feature_set: str = DEFAULT_FEATURE_SET) -> List[Dict]:
    with store.connect() as db:
        rows=db.execute("""
          SELECT f.exchange,f.symbol,f.timestamp,f.features,f.label,f.label_return
          FROM feature_rows f
          JOIN (SELECT exchange,symbol,MAX(timestamp) timestamp FROM feature_rows WHERE feature_set=? GROUP BY exchange,symbol) latest
          ON f.exchange=latest.exchange AND f.symbol=latest.symbol AND f.timestamp=latest.timestamp
          WHERE f.feature_set=? ORDER BY f.exchange,f.symbol
        """,(feature_set,feature_set)).fetchall()
    return [{**dict(x),"features":_decode_json(x["features"])} for x in rows]


def estimated_trade_cost_bps(price: float, quantity: int, average_daily_value: float,
                             base_charges_bps: float = 12.0) -> Dict:
    notional=max(0,price*quantity); participation=notional/max(1,average_daily_value)
    slippage=4.0+min(60.0,math.sqrt(participation)*100)
    impact=min(80.0,participation*10000*.10)
    return {"charges_bps":base_charges_bps,"charge_components":["brokerage","STT/CTT","exchange transaction charge","SEBI fee","GST","stamp duty/DP where applicable"],"slippage_bps":round(slippage,2),"impact_bps":round(impact,2),"total_bps":round(base_charges_bps+slippage+impact,2),"participation_pct":round(participation*100,4),"note":"Configurable estimate; rates vary by segment/date and must be reconciled against actual broker contract notes."}


def walk_forward_validate(store: Optional[ResearchStore] = None, initial_train: int = 0, test_window: int = 0, capital: float = 1_000_000) -> Dict:
    store=store or ResearchStore(); active=validation_model(store)
    if not active: raise ValueError("Train and activate a research model first")
    feature_set=active["feature_set"]; features=FEATURE_SETS[feature_set]; purge=10 if feature_set=="daily_v2" else 5
    samples=_load_samples(store,feature_set)
    dates=sorted(set(x["timestamp"] for x in samples))
    initial_train=initial_train or max(120,int(len(dates)*.40)); test_window=test_window or max(30,int(len(dates)*.10))
    if len(dates)<initial_train+test_window: raise ValueError("Insufficient chronological dates for one walk-forward fold")
    folds=[]; trades=[]; equity=capital; peak=capital; max_dd=0.0
    cursor=initial_train
    while cursor+max(10,test_window//2)<=len(dates):
        train_dates=set(dates[:max(1,cursor-purge)]); test_dates=set(dates[cursor:min(len(dates),cursor+test_window)])
        train=[x for x in samples if x["timestamp"] in train_dates]; test=[x for x in samples if x["timestamp"] in test_dates]
        model=_fit_chronologically_calibrated(train,active["algorithm"],features,purge,fast=True) if active["payload"].get("calibration") else _fit_algorithm(train,active["algorithm"],features,fast=True)
        fold_correct=0; fold_selected=0; fold_trades=0
        fold_dates=sorted(test_dates)
        for date_index,trade_date in enumerate(fold_dates):
            if date_index % purge:  # do not overlap the forward barrier horizon
                continue
            candidates=[]; use_policy=bool(active["payload"].get("trading_policy"))
            for row in (item for item in test if item["timestamp"] == trade_date):
                raw_probability=_raw_predict(model,row["features"])
                probability=apply_calibration(raw_probability,model.get("calibration",{}))
                allocation=min(equity*.05,capital*.05)
                cost_bps=estimated_trade_cost_bps(1,int(allocation),row["features"]["average_daily_value_20d"])["total_bps"]
                candidates.append(score_candidate(row,probability,raw_probability,cost_bps))
            selected=rank_candidates(candidates,10) if use_policy else sorted(
                (item for item in candidates if item.probability>=.55 or item.probability<=.45),
                key=lambda item:(abs(item.probability-.5),abs(item.raw_probability-.5)),reverse=True)[:10]
            day_equity=equity; day_pnl=0.0
            for candidate in selected:
                probability,signal,row=candidate.probability,candidate.signal,candidate.row
                fold_selected+=1; fold_correct+=int((row["label_return"]>0)==(signal>0))
                allocation=min(day_equity*.05,capital*.05)
                cost=candidate.estimated_cost_bps/10000
                net=signal*row["label_return"]-cost; pnl=allocation*net; day_pnl+=pnl; fold_trades+=1
                trades.append({"timestamp":row["timestamp"],"exchange":row["exchange"],"symbol":row["symbol"],"signal":signal,
                    "probability":round(probability,4),"regime":candidate.regime,"expected_net_edge_bps":candidate.expected_net_edge_bps,
                    "estimated_cost_bps":candidate.estimated_cost_bps,"gross_return":row["label_return"],"net_pnl":round(pnl,2)})
            equity=max(0.0,equity+day_pnl); peak=max(peak,equity); max_dd=min(max_dd,equity/peak-1)
        folds.append({"train_samples":len(train),"test_samples":len(test),"purge_sessions":purge,"rebalance_sessions":purge,"top_signals":10,"max_gross_exposure_pct":50,"trades":fold_trades,"directional_accuracy":round(fold_correct/max(1,fold_selected)*100,2)})
        cursor+=test_window
    wins=[x for x in trades if x["net_pnl"]>0]; losses=[x for x in trades if x["net_pnl"]<=0]; gp=sum(x["net_pnl"] for x in wins); gl=abs(sum(x["net_pnl"] for x in losses))
    policy=active["payload"].get("trading_policy")
    metrics={"folds":len(folds),"symbols":len(set((x["exchange"],x["symbol"]) for x in samples)),"trades":len(trades),"ending_capital":round(equity,2),"return_pct":round((equity/capital-1)*100,2),"win_rate":round(len(wins)/max(1,len(trades))*100,2),"profit_factor":round(gp/gl,2) if gl else None,"max_drawdown_pct":round(max_dd*100,2),"portfolio_policy":(f"cost-aware top 10, regime gate, no-trade zone, {purge}-session non-overlapping rebalance, 5% each, 50% gross cap" if policy else f"top 10 calibrated signals, {purge}-session non-overlapping rebalance, 5% each, 50% gross cap"),"cost_model":"charges + slippage + participation impact","trading_policy":policy}
    model=active; version=model["version"]
    checks={"positive_net_return":metrics["return_pct"]>0,"profit_factor_at_least_1_20":(metrics["profit_factor"] or 0)>=1.20,
            "max_drawdown_not_below_15_pct":metrics["max_drawdown_pct"]>=-15,"at_least_five_folds":metrics["folds"]>=5,
            "untouched_log_loss_below_random":float(model["metrics"].get("holdout",{}).get("log_loss",1))<.693,
            "resolved_forward_shadow_months":False,"instrument_executability_verified":False}
    metrics["promotion_gate"]={"passed":all(checks.values()),"checks":checks,"live_eligible":False}
    payload=dict(model["payload"]); payload["promotion_gate"]=metrics["promotion_gate"]; payload["live_eligible"]=False
    with store.connect() as db:
        db.execute("INSERT INTO validation_runs(model_version,validation_type,period_start,period_end,symbols,trades,metrics,created_at) VALUES(?,?,?,?,?,?,?,?)",(version,"purged_expanding_walk_forward",dates[initial_train],dates[-1],metrics["symbols"],len(trades),json.dumps({"summary":metrics,"folds":folds}),now_iso()))
        db.execute("UPDATE model_versions SET payload=? WHERE version=?",(json.dumps(payload),version))
        promotion={"promoted":False,"reason":"active baseline revalidated"}
        if model.get("status")=="candidate":
            baseline=db.execute("SELECT * FROM model_versions WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()
            baseline_summary={}
            if baseline:
                prior=db.execute("SELECT metrics FROM validation_runs WHERE model_version=? ORDER BY id DESC LIMIT 1",(baseline["version"],)).fetchone()
                if prior: baseline_summary=_decode_json(prior["metrics"]).get("summary",{})
            candidate_pf=float(metrics["profit_factor"] or 0); baseline_pf=float(baseline_summary.get("profit_factor",0) or 0)
            candidate_loss=float(model["metrics"].get("holdout",{}).get("log_loss",999))
            baseline_metrics=_decode_json(baseline["metrics"]) if baseline else {}
            baseline_loss=float(baseline_metrics.get("holdout",{}).get("log_loss",999))
            better=(not baseline) or (candidate_pf>=baseline_pf+.02 and metrics["return_pct"]>float(baseline_summary.get("return_pct",0))
                and metrics["max_drawdown_pct"]>=float(baseline_summary.get("max_drawdown_pct",-100))-2 and candidate_loss<=baseline_loss+.001)
            if better:
                db.execute("UPDATE model_versions SET status='archived' WHERE status='active'")
                db.execute("UPDATE model_versions SET status='active' WHERE version=?",(version,))
                promotion={"promoted":True,"reason":"candidate outperformed the paper baseline under locked comparison rules"}
            else:
                db.execute("UPDATE model_versions SET status='rejected' WHERE version=?",(version,))
                promotion={"promoted":False,"reason":"candidate did not outperform the active paper baseline",
                           "baseline_profit_factor":baseline_pf,"candidate_profit_factor":candidate_pf}
    return {"model_version":version,"summary":metrics,"folds":folds,"recent_trades":trades[-10:],"candidate_promotion":promotion,"warning":"Research result, not a profit guarantee."}


def generate_shadow_predictions(store: Optional[ResearchStore] = None) -> Dict:
    store=store or ResearchStore(); model=active_model(store)
    if not model: raise ValueError("Train and activate a model first")
    samples=_load_all_features(store,model["feature_set"]); latest={}
    for row in samples: latest[(row["exchange"],row["symbol"])]=row
    created=0
    with store.connect() as db:
        for row in latest.values():
            raw_probability=_raw_predict(model["payload"],row["features"])
            probability=apply_calibration(raw_probability,model["payload"].get("calibration",{}))
            cost_bps=estimated_trade_cost_bps(1,10000,row["features"]["average_daily_value_20d"])["total_bps"]
            candidate=score_candidate(row,probability,raw_probability,cost_bps)
            signal=(candidate.signal if candidate.accepted else 0) if model["payload"].get("trading_policy") else (1 if probability>=.55 else -1 if probability<=.45 else 0)
            db.execute("""INSERT INTO shadow_predictions(model_version,exchange,symbol,timestamp,probability,signal,realised_return,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(model_version,exchange,symbol,timestamp) DO UPDATE SET probability=EXCLUDED.probability,signal=EXCLUDED.signal,
                realised_return=EXCLUDED.realised_return,status=EXCLUDED.status,created_at=EXCLUDED.created_at""",
                (model["version"],row["exchange"],row["symbol"],row["timestamp"],probability,signal,None,"PENDING",now_iso())); created+=1
    return {"model_version":model["version"],"predictions":created,"mode":"shadow","orders_allowed":False}


def resolve_shadow_predictions(store: Optional[ResearchStore] = None) -> Dict:
    store = store or ResearchStore()
    resolved = 0
    if getattr(store, "is_postgres", False):
        from .tasks.worker import DATABASE_URL
        from sqlalchemy import create_engine, text
        engine = create_engine(DATABASE_URL)
        with engine.begin() as conn:
            # 1. Resolve from shadow_execution_audits (realised trade exits)
            r1 = conn.execute(text("""
                UPDATE shadow_predictions p
                SET realised_return = CASE 
                    WHEN a.side = 'BUY' THEN (a.realised_exit_price - a.theoretical_fill_price) / NULLIF(a.theoretical_fill_price, 0)
                    ELSE (a.theoretical_fill_price - a.realised_exit_price) / NULLIF(a.theoretical_fill_price, 0)
                  END,
                  status = 'RESOLVED'
                FROM shadow_execution_audits a
                WHERE p.status = 'PENDING'
                  AND a.instrument_id = p.instrument_id
                  AND a.signal_at = p.timestamp
                  AND a.realised_exit_price IS NOT NULL
                  AND a.theoretical_fill_price IS NOT NULL
                  AND a.theoretical_fill_price > 0
            """))
            # 2. Resolve intraday predictions from future live_market_bars (15-30m forward)
            r2 = conn.execute(text("""
                UPDATE shadow_predictions p
                SET realised_return = (sub.close_price - p.decision_price) / p.decision_price,
                    status = 'RESOLVED'
                FROM (
                    SELECT DISTINCT ON (p.id)
                        p.id,
                        b.close_price
                    FROM shadow_predictions p
                    JOIN live_market_bars b 
                      ON b.instrument_id = p.instrument_id 
                     AND b.interval IN ('5minute', '1minute')
                     AND b.bar_time >= p.timestamp + INTERVAL '15 minutes'
                     AND b.bar_time <= p.timestamp + INTERVAL '30 minutes'
                    WHERE p.status = 'PENDING'
                      AND p.decision_price IS NOT NULL 
                      AND p.decision_price > 0
                    ORDER BY p.id, b.bar_time ASC
                ) sub
                WHERE p.id = sub.id
            """))
            # 3. Resolve daily EOD predictions from forward daily bars (10-25 days forward)
            r3 = conn.execute(text("""
                WITH resolved_daily AS (
                    SELECT DISTINCT ON (p.id)
                        p.id,
                        (b1.close_price - b0.close_price) / NULLIF(b0.close_price, 0) as realised_return
                    FROM shadow_predictions p
                    JOIN instrument_master i ON i.exchange = p.exchange AND i.symbol = p.symbol
                    JOIN live_market_bars b0 
                      ON b0.instrument_id = i.id 
                     AND b0.interval = 'day' 
                     AND b0.bar_time = p.timestamp
                    JOIN live_market_bars b1 
                      ON b1.instrument_id = i.id 
                     AND b1.interval = 'day' 
                     AND b1.bar_time >= p.timestamp + INTERVAL '10 days'
                     AND b1.bar_time <= p.timestamp + INTERVAL '25 days'
                    WHERE p.status = 'PENDING'
                      AND p.timeframe IS NULL
                      AND b0.close_price > 0
                    ORDER BY p.id, b1.bar_time ASC
                )
                UPDATE shadow_predictions p
                SET realised_return = rd.realised_return,
                    status = 'RESOLVED'
                FROM resolved_daily rd
                WHERE p.id = rd.id
            """))
            # 4. Resolve 15:30 EOD 5m predictions from next-morning open bars
            r4 = conn.execute(text("""
                WITH next_day_bars AS (
                    SELECT DISTINCT ON (p.id)
                        p.id,
                        (b.close_price - p.decision_price) / p.decision_price as realised_return
                    FROM shadow_predictions p
                    JOIN live_market_bars b 
                      ON b.instrument_id = p.instrument_id 
                     AND b.interval IN ('5minute', '1minute')
                     AND b.bar_time >= p.timestamp + INTERVAL '12 hours'
                     AND b.bar_time <= p.timestamp + INTERVAL '24 hours'
                    WHERE p.status = 'PENDING'
                      AND p.timeframe = '5minute'
                      AND p.decision_price IS NOT NULL 
                      AND p.decision_price > 0
                    ORDER BY p.id, b.bar_time ASC
                )
                UPDATE shadow_predictions p
                SET realised_return = nd.realised_return,
                    status = 'RESOLVED'
                FROM next_day_bars nd
                WHERE p.id = nd.id
            """))
            # 5. Resolve from feature_rows (daily EOD)
            r5 = conn.execute(text("""
                UPDATE shadow_predictions p
                SET realised_return = f.label_return,
                    status = 'RESOLVED'
                FROM feature_rows f
                WHERE p.status = 'PENDING'
                  AND f.exchange = p.exchange
                  AND f.symbol = p.symbol
                  AND f.timestamp = p.timestamp
                  AND f.label_return IS NOT NULL
            """))
            resolved = (r1.rowcount or 0) + (r2.rowcount or 0) + (r3.rowcount or 0) + (r4.rowcount or 0) + (r5.rowcount or 0)

        with engine.connect() as conn:
            pending_count = conn.execute(text("SELECT count(*) FROM shadow_predictions WHERE status = 'PENDING'")).scalar()
            total_resolved = conn.execute(text("SELECT count(*) FROM shadow_predictions WHERE status = 'RESOLVED'")).scalar()
        return {"resolved": int(total_resolved), "pending": int(pending_count), "newly_resolved": resolved}

    with store.connect() as db:
        pending = db.execute("SELECT id,exchange,symbol,timestamp,signal FROM shadow_predictions WHERE status='PENDING'").fetchall()
        for row in pending:
            feature = db.execute("SELECT label_return FROM feature_rows WHERE exchange=? AND symbol=? AND timestamp=? AND label_return IS NOT NULL ORDER BY feature_set LIMIT 1", (row["exchange"], row["symbol"], row["timestamp"])).fetchone()
            if feature:
                db.execute("UPDATE shadow_predictions SET realised_return=?,status='RESOLVED' WHERE id=?", (feature["label_return"], row["id"]))
                resolved += 1
    return {"resolved": resolved, "pending": len(pending) - resolved}


def detect_drift(store: Optional[ResearchStore] = None, window: int = 60) -> Dict:
    store=store or ResearchStore(); model=active_model(store)
    if not model: raise ValueError("Train and activate a model first")
    samples=_load_samples(store,model["feature_set"])[-window:]; reports=[]
    for feature in model["payload"]["features"]:
        current=mean([x["features"][feature] for x in samples]); reference=model["payload"]["reference"][feature]; shift=abs(current-reference["mean"])/max(reference["std"],1e-9); status="alert" if shift>=2 else "watch" if shift>=1 else "stable"
        reports.append({"feature":feature,"reference_mean":reference["mean"],"current_mean":current,"standardised_shift":shift,"status":status})
    with store.connect() as db:
        db.executemany("INSERT INTO drift_reports(model_version,feature,reference_mean,current_mean,standardised_shift,status,created_at) VALUES(?,?,?,?,?,?,?)",[(model["version"],x["feature"],x["reference_mean"],x["current_mean"],x["standardised_shift"],x["status"],now_iso()) for x in reports])
    return {"model_version":model["version"],"window":len(samples),"overall":"alert" if any(x["status"]=="alert" for x in reports) else "watch" if any(x["status"]=="watch" for x in reports) else "stable","features":reports}


def pipeline_status() -> Dict:
    history=HistoryStore().stats(); research=ResearchStore().status()
    return {"history":history,"research":research,"capabilities":{"daily_and_intraday_ingestion":True,"corporate_action_adjustment":True,"official_action_sync":True,"point_in_time_universe_schema":True,"regime_features":True,"triple_barrier_labels":True,"gradient_boosting_comparison":True,"dnn_mlp_classifier_candidate":True,"soft_voting_ensemble_candidate":True,"isotonic_probability_calibration":True,"cost_aware_cross_sectional_ranking":True,"regime_gating":True,"strict_no_trade_zone":True,"untouched_final_holdout":True,"model_registry":True,"purged_walk_forward":True,"cost_and_liquidity_model":True,"portfolio_oos":True,"shadow_mode":True,"adaptive_reward_memory":True,"drift_detection":True,"scheduled_retraining":True},"limitations":["Free archive coverage is end-of-day; licensed intraday/tick history is not included","India VIX history is not yet connected; market-volatility proxies are used instead","Equity open interest is unavailable in the current EOD archive and is not fabricated","Point-in-time delisted membership is still unavailable","BSE actions are mirrored from NSE only when ISINs match","Real-cost promotion accepts only reconciled Zerodha contract-note evidence","Derivative executability requires broker-connected shadow observations","Shadow validation needs 90 genuine future market sessions","Current model remains below the live promotion gate","DNN/ensemble candidates can improve pattern detection only if validated; they are not profit guarantees"],"updated_at":now_iso()}

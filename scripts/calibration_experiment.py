"""Chronological calibration comparison; research-only and never changes the active model."""
import base64
import json
import math
import pickle
import sys
from pathlib import Path

import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression

ROOT=Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from backend.ml_pipeline import ResearchStore, _load_samples, active_model


def _loss(labels,probabilities):
    p=np.clip(np.asarray(probabilities,dtype=float),1e-6,1-1e-6); y=np.asarray(labels,dtype=float)
    return float(-np.mean(y*np.log(p)+(1-y)*np.log(1-p)))


def _brier(labels,probabilities):
    return float(np.mean((np.asarray(probabilities)-np.asarray(labels))**2))


def _matrix(rows,features): return np.asarray([[float(row["features"][name]) for name in features] for row in rows],dtype=np.float64)


def run():
    store=ResearchStore(); active=active_model(store); samples=_load_samples(store,active["feature_set"]); features=active["payload"]["features"]
    start,end=active["metrics"]["selection_period"]; calibration=[row for row in samples if start<=row["timestamp"]<=end]
    hstart,hend=active["metrics"]["final_period"]; holdout=[row for row in samples if hstart<=row["timestamp"]<=hend]
    dates=sorted({row["timestamp"] for row in calibration}); midpoint=dates[len(dates)//2]
    fit=[row for row in calibration if row["timestamp"]<midpoint]; select=[row for row in calibration if row["timestamp"]>=midpoint]
    estimator=pickle.loads(base64.b64decode(active["payload"]["estimator_b64"]))
    raw_fit=estimator.predict_proba(_matrix(fit,features))[:,1]; raw_select=estimator.predict_proba(_matrix(select,features))[:,1]; raw_holdout=estimator.predict_proba(_matrix(holdout,features))[:,1]
    y_fit=np.asarray([row["label"] for row in fit]); y_select=np.asarray([row["label"] for row in select]); y_holdout=np.asarray([row["label"] for row in holdout])
    methods={"identity":(raw_select,raw_holdout)}
    platt=LogisticRegression(C=1,max_iter=1000,solver="liblinear").fit(np.log(np.clip(raw_fit,1e-6,1-1e-6)/(1-np.clip(raw_fit,1e-6,1-1e-6))).reshape(-1,1),y_fit)
    transform=lambda p:np.log(np.clip(p,1e-6,1-1e-6)/(1-np.clip(p,1e-6,1-1e-6))).reshape(-1,1)
    methods["platt"]=(platt.predict_proba(transform(raw_select))[:,1],platt.predict_proba(transform(raw_holdout))[:,1])
    beta_features=lambda p:np.column_stack([np.log(np.clip(p,1e-6,1)), -np.log(np.clip(1-p,1e-6,1))])
    beta=LogisticRegression(C=1,max_iter=1000,solver="liblinear").fit(beta_features(raw_fit),y_fit)
    methods["beta"]=(beta.predict_proba(beta_features(raw_select))[:,1],beta.predict_proba(beta_features(raw_holdout))[:,1])
    isotonic=IsotonicRegression(out_of_bounds="clip",y_min=1e-4,y_max=1-1e-4).fit(raw_fit,y_fit)
    methods["isotonic"]=(isotonic.predict(raw_select),isotonic.predict(raw_holdout))
    scores=[]
    for name,(selection,final) in methods.items():
        scores.append({"method":name,"selection_log_loss":round(_loss(y_select,selection),6),"selection_brier":round(_brier(y_select,selection),6),
                       "holdout_log_loss":round(_loss(y_holdout,final),6),"holdout_brier":round(_brier(y_holdout,final),6)})
    winner=min(scores,key=lambda row:(row["selection_log_loss"],row["selection_brier"]))
    return {"model_version":active["version"],"fit_samples":len(fit),"selection_samples":len(select),"retrospective_holdout_samples":len(holdout),
            "selection_rule":"minimum chronological selection log loss; holdout not used for method selection","winner":winner["method"],"scores":scores,
            "warning":"The former holdout has already been reported in prior experiments; improvement here is retrospective and cannot clear the promotion gate."}


if __name__=="__main__": print(json.dumps(run(),indent=2))

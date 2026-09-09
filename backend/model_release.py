"""Explicit, secret-safe v2.5 retrain/sign/verify release workflow."""
import argparse
import hashlib
import hmac
import json
import os

from sqlalchemy import create_engine, text

from .config import DATABASE_URL, MODEL_ARTIFACT_KEY
from .ml_pipeline import active_model, train_model, validation_model, walk_forward_validate
from .model_feedback import export_high_quality_shadow_feedback


def verify_model(model=None):
    model=model or active_model()
    if not model: raise RuntimeError("No active model")
    payload=model["payload"]; encoded=payload.get("estimator_b64",""); signature=payload.get("estimator_hmac","")
    if not encoded or not signature: return {"model_version":model["version"],"signature_verified":False,"reason":"unsigned or missing estimator"}
    expected=hmac.new(MODEL_ARTIFACT_KEY.encode(),encoded.encode(),hashlib.sha256).hexdigest()
    return {"model_version":model["version"],"signature_verified":hmac.compare_digest(signature,expected),
            "artifact_sha256":hashlib.sha256(encoded.encode()).hexdigest()}


def verify_active(): return verify_model(active_model())


def _require_model_key():
    if len(MODEL_ARTIFACT_KEY)<32 or MODEL_ARTIFACT_KEY.startswith("local-paper-") or MODEL_ARTIFACT_KEY==os.getenv("NIVESH_SECRET"):
        raise RuntimeError("A separate production model-artifact key of at least 32 characters is required")


def resign_active_baseline():
    if os.getenv("MODEL_RELEASE_CONFIRM")!="SIGN_ACTIVE_BASELINE":
        raise RuntimeError("MODEL_RELEASE_CONFIRM=SIGN_ACTIVE_BASELINE is required")
    _require_model_key()
    model=active_model()
    if not model: raise RuntimeError("No active model")
    payload=dict(model["payload"])
    encoded=payload.get("estimator_b64","")
    if not encoded: raise RuntimeError("Active model has no serialised estimator to sign")
    payload["estimator_hmac"]=hmac.new(MODEL_ARTIFACT_KEY.encode(),encoded.encode(),hashlib.sha256).hexdigest()
    payload["live_eligible"]=False
    verification=verify_model({**model,"payload":payload})
    if not verification["signature_verified"]: raise RuntimeError("Active model signature verification failed")
    holdout=model["metrics"].get("holdout",{})
    manifest={"model_version":model["version"],"action":"resign_active_baseline","signature":verification,
              "live_eligible":False,"note":"Signature-only operation; no retraining, promotion, or live-trading eligibility change."}
    engine=create_engine(DATABASE_URL,pool_pre_ping=True,future=True)
    with engine.begin() as connection:
        connection.execute(text("UPDATE model_versions SET payload=CAST(:payload AS jsonb) WHERE version=:version"),
                           {"payload":json.dumps(payload),"version":model["version"]})
        connection.execute(text("""INSERT INTO model_release_manifests(model_version,feature_set,algorithm,training_samples,holdout_log_loss,
            artifact_sha256,key_fingerprint,signature_verified,promotion_eligible,manifest)
            VALUES(:version,:feature_set,:algorithm,:samples,:loss,:artifact,:fingerprint,TRUE,FALSE,CAST(:manifest AS jsonb))
            ON CONFLICT(model_version) DO UPDATE SET signature_verified=TRUE,artifact_sha256=EXCLUDED.artifact_sha256,
            key_fingerprint=EXCLUDED.key_fingerprint,manifest=EXCLUDED.manifest,released_at=CURRENT_TIMESTAMP"""),
            {"version":model["version"],"feature_set":model["feature_set"],"algorithm":model["algorithm"],"samples":model["training_samples"],
             "loss":holdout.get("log_loss"),"artifact":verification["artifact_sha256"],
             "fingerprint":hashlib.sha256(MODEL_ARTIFACT_KEY.encode()).hexdigest()[:16],"manifest":json.dumps(manifest)})
    return manifest


def retrain_and_release():
    if os.getenv("MODEL_RELEASE_CONFIRM")!="RETRAIN_AND_SIGN": raise RuntimeError("MODEL_RELEASE_CONFIRM=RETRAIN_AND_SIGN is required")
    _require_model_key()
    quality_feedback=export_high_quality_shadow_feedback(DATABASE_URL) if os.getenv("NIVESH_ML_QUALITY_FEEDBACK_ENABLED","1")=="1" else {"status":"disabled"}
    before=active_model(); trained=train_model(); candidate=validation_model(); verification=verify_model(candidate)
    if not verification["signature_verified"]: raise RuntimeError("New model signature verification failed")
    validation=walk_forward_validate(); model=active_model()
    if not model or model["version"]!=trained["version"]:
        raise RuntimeError("Candidate failed paper-baseline comparison and was not promoted")
    holdout=model["metrics"].get("holdout",{})
    manifest={"model_version":model["version"],"previous_version":before["version"] if before else None,"trained":trained,
              "validation_summary":validation["summary"],"signature":verification,"live_eligible":False,
              "quality_feedback":quality_feedback,
              "quality_feedback_policy":"Closed paper trades are exported only when quality >= NIVESH_ML_FEEDBACK_MIN_QUALITY. Historical estimator promotion still requires baseline comparison and forward gates."}
    engine=create_engine(DATABASE_URL,pool_pre_ping=True,future=True)
    with engine.begin() as connection:
        connection.execute(text("""INSERT INTO model_release_manifests(model_version,feature_set,algorithm,training_samples,holdout_log_loss,
            artifact_sha256,key_fingerprint,signature_verified,promotion_eligible,manifest)
            VALUES(:version,:feature_set,:algorithm,:samples,:loss,:artifact,:fingerprint,TRUE,FALSE,CAST(:manifest AS jsonb))
            ON CONFLICT(model_version) DO UPDATE SET signature_verified=TRUE,manifest=EXCLUDED.manifest,released_at=CURRENT_TIMESTAMP"""),
            {"version":model["version"],"feature_set":model["feature_set"],"algorithm":model["algorithm"],"samples":model["training_samples"],
             "loss":holdout.get("log_loss"),"artifact":verification["artifact_sha256"],
             "fingerprint":hashlib.sha256(MODEL_ARTIFACT_KEY.encode()).hexdigest()[:16],"manifest":json.dumps(manifest)})
    return manifest


def main():
    parser=argparse.ArgumentParser(); parser.add_argument("action",choices=["verify","retrain","resign-active"]); args=parser.parse_args()
    actions={"verify":verify_active,"retrain":retrain_and_release,"resign-active":resign_active_baseline}
    print(json.dumps(actions[args.action](),indent=2,default=str))


if __name__=="__main__": main()

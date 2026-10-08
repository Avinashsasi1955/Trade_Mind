# Historical Documentation Archive Index (INDEX.md)

*Archived as part of Single Source of Truth Consolidation on 2026-10-08*  
*Canonical Living Documents are strictly maintained at repository root: `STATUS.md`, `MASTER_COMPREHENSIVE_PLAN.md`, and `README.md`.*

---

## Catalog of Retired & Archived Documents

| Document | Original Date / Phase | Purpose & Retirement Reason |
| :--- | :--- | :--- |
| `CLOUD_DEPLOYMENT_READINESS.md` | Jul 2026 | Cloud deployment checklist; superseded by root `README.md`. |
| `FINAL_PHASE_HANDOFF.md` | Jul 2026 | Milestone handoff notes; retired after Phase C implementation. |
| `IMPLEMENTATION_PLAN_PHASES_A_B_C.md` | Jul–Aug 2026 | Early multi-phase roadmap; superseded by `MASTER_COMPREHENSIVE_PLAN.md`. |
| `INFRASTRUCTURE_STATUS.md` | Jul 2026 | Early infra readiness; superseded by automated `STATUS.md`. |
| `LATEST_MODEL_METRICS.md` | Jul 4, 2026 | Static report of v2.5 walk-forward metrics (+27.90% / PF 1.31); superseded by direct DB metrics. |
| `LIVE_PAPER_PHASES_1_3_REPORT.md` | Jul 2026 | Historical paper testing report; superseded by live audit tables. |
| `LIVE_V3_DEPLOYMENT.md` | Jul 2026 | v3 deployment checklist; superseded by root `README.md`. |
| `MASTER_ROADMAP.md` | Jun 2026 | Early project roadmap; superseded by `MASTER_COMPREHENSIVE_PLAN.md`. |
| `ML_READINESS_REPORT.md` | Jul 2026 | Early ML assessment; superseded by direct DB metrics in `STATUS.md`. |
| `ML_V2_5_REPORT.md` | Jul 1, 2026 | Initial training report for v2.5 documenting untouched holdout log-loss 0.6937. |
| `NIVESH_AI_COMPREHENSIVE_SPECIFICATION.md` | Oct 8, 2026 | Aspirational product specification; retired to eliminate drift with live runtime code. |
| `PRODUCTION_READINESS.md` | Jul 2026 | Pre-production audits; superseded by unit test suite and root `STATUS.md`. |
| `PROMOTION_ENGINE_V3_4_REPORT.md` | Jul 2026 | Walk-forward promotion gate documentation; superseded by `backend/ml/validation_engine.py`. |
| `RECENT_MODIFICATIONS_WALKTHROUGH.md` | Oct 2026 | Development session walkthrough notes. |
| `REMAINING_PHASES_V3_7_REPORT.md` | Jul 2026 | Intermediate phase tracking report. |
| `SECURITY_HARDENING_REPORT.md` | Jul 2026 | Security audit and ALB/JWT hardening verification. |
| `SPX_GEX_VP_SHAPES_TIMEFRAME_ANALYSIS.md`| Sep 2026 | Research analysis on GEX, VP shapes, and MTF fail-open vulnerabilities. |
| `V3_2_FULL_REPORT.md` | Jul 2, 2026 | Comprehensive v3.2 model report documenting 6-fold walk-forward results. |
| `V3_8_MODEL_AND_GATE_REPORT.md` | Jul 4, 2026 | Milestone report recording explicit rejection of first v2.6 candidate. |
| `VS_CODE_RUN_GUIDE.md` | Jul 2026 | Local developer environment setup notes. |
| `memory.md` | Oct 8, 2026 | Working session memory; retired from root to prevent documentation drift. |
| `rules.md` | Oct 8, 2026 | Development rules; retired to prevent contradiction with runtime `.env` values. |
| `task.md` | Oct 8, 2026 | Task tracking document; retired to maintain strict 3-file root boundary. |

---

## Authority Notice
All operational parameters, database queries, and deployment commands defer strictly to:
1. [STATUS.md](../../STATUS.md) (dynamically generated from live database, git commit, and runtime config)
2. [MASTER_COMPREHENSIVE_PLAN.md](../../MASTER_COMPREHENSIVE_PLAN.md)
3. [README.md](../../README.md)

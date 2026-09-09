"""Command-line research jobs. No command can place an order."""
import argparse
import json

from pathlib import Path

from .ml_pipeline import build_research_dataset, detect_drift, generate_shadow_predictions, import_corporate_actions, import_universe_membership, pipeline_status, prune_research_dataset, resolve_shadow_predictions, train_model, walk_forward_validate


def main():
    parser=argparse.ArgumentParser(); parser.add_argument("job",choices=["build","prune","train","validate","shadow","resolve-shadow","drift","all","status","import-actions","import-universe"]); parser.add_argument("--file",default=""); args=parser.parse_args()
    output={}
    if args.job in {"build","all"}: output["dataset"]=build_research_dataset()
    if args.job == "prune": output["prune"]=prune_research_dataset()
    if args.job in {"train","all"}: output["model"]=train_model()
    if args.job in {"validate","all"}: output["validation"]=walk_forward_validate()
    if args.job in {"shadow","all"}: output["shadow"]=generate_shadow_predictions()
    if args.job=="resolve-shadow": output["shadow_resolution"]=resolve_shadow_predictions()
    if args.job in {"drift","all"}: output["drift"]=detect_drift()
    if args.job=="status": output=pipeline_status()
    if args.job=="import-actions":
        if not args.file: parser.error("--file is required")
        output={"corporate_actions_imported":import_corporate_actions(Path(args.file))}
    if args.job=="import-universe":
        if not args.file: parser.error("--file is required")
        output={"universe_memberships_imported":import_universe_membership(Path(args.file))}
    print(json.dumps(output,indent=2))


if __name__=="__main__": main()

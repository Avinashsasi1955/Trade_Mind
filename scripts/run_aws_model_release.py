"""Launch the one-shot ECS retrain/sign task after explicit operator approval."""
import argparse
import json
import subprocess
from pathlib import Path

import boto3


def outputs(infra: Path):
    result=subprocess.run(["tofu","-chdir="+str(infra),"output","-json"],check=True,capture_output=True,text=True)
    return {key:item["value"] for key,item in json.loads(result.stdout).items()}


def run(infra: Path,confirm: str):
    if confirm!="RETRAIN_AND_SIGN": raise RuntimeError("--confirm RETRAIN_AND_SIGN is required")
    values=outputs(infra); ecs=boto3.client("ecs",region_name="ap-south-1")
    response=ecs.run_task(cluster=values["ecs_cluster_name"],taskDefinition=values["model_release_task_definition_arn"],launchType="FARGATE",
        count=1,networkConfiguration={"awsvpcConfiguration":{"subnets":values["private_subnet_ids"],
        "securityGroups":[values["application_security_group_id"]],"assignPublicIp":"DISABLED"}})
    if response.get("failures"): raise RuntimeError("ECS rejected model release: "+json.dumps(response["failures"]))
    task_arn=response["tasks"][0]["taskArn"]
    ecs.get_waiter("tasks_stopped").wait(cluster=values["ecs_cluster_name"],tasks=[task_arn],WaiterConfig={"Delay":15,"MaxAttempts":480})
    task=ecs.describe_tasks(cluster=values["ecs_cluster_name"],tasks=[task_arn])["tasks"][0]; container=task["containers"][0]
    if container.get("exitCode")!=0: raise RuntimeError(f"Model release failed: {container.get('reason') or task.get('stoppedReason')}")
    return {"task_arn":task_arn,"exit_code":0,"manifest_location":"PostgreSQL model_release_manifests",
            "promotion_changed":False,"operator_review_required":True}


if __name__=="__main__":
    parser=argparse.ArgumentParser(); parser.add_argument("--infra",type=Path,default=Path("infra/aws-ha")); parser.add_argument("--confirm",required=True)
    args=parser.parse_args(); print(json.dumps(run(args.infra.resolve(),args.confirm),indent=2))

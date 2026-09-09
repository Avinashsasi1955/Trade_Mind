"""Launch and verify the one-shot private ECS restore task after provisioning."""
import argparse
import json
import subprocess
from pathlib import Path

import boto3


def outputs(infra: Path):
    result=subprocess.run(["tofu","-chdir="+str(infra),"output","-json"],check=True,capture_output=True,text=True)
    return {key:item["value"] for key,item in json.loads(result.stdout).items()}


def run(infra: Path):
    values=outputs(infra); ecs=boto3.client("ecs",region_name="ap-south-1")
    response=ecs.run_task(cluster=values["ecs_cluster_name"],taskDefinition=values["restore_task_definition_arn"],launchType="FARGATE",
        count=1,networkConfiguration={"awsvpcConfiguration":{"subnets":values["private_subnet_ids"],
        "securityGroups":[values["application_security_group_id"]],"assignPublicIp":"DISABLED"}})
    if response.get("failures"): raise RuntimeError("ECS rejected the restore task: "+json.dumps(response["failures"]))
    task_arn=response["tasks"][0]["taskArn"]
    ecs.get_waiter("tasks_stopped").wait(cluster=values["ecs_cluster_name"],tasks=[task_arn],WaiterConfig={"Delay":15,"MaxAttempts":240})
    task=ecs.describe_tasks(cluster=values["ecs_cluster_name"],tasks=[task_arn])["tasks"][0]; container=task["containers"][0]
    exit_code=container.get("exitCode")
    if exit_code!=0: raise RuntimeError(f"Restore task failed with exit code {exit_code}: {container.get('reason') or task.get('stoppedReason')}")
    return {"task_arn":task_arn,"exit_code":exit_code,"stopped_reason":task.get("stoppedReason"),"cloudwatch_group":"/ecs/nivesh-production","verified":True}


if __name__=="__main__":
    parser=argparse.ArgumentParser(); parser.add_argument("--infra",type=Path,default=Path("infra/aws-ha")); args=parser.parse_args()
    print(json.dumps(run(args.infra.resolve()),indent=2))

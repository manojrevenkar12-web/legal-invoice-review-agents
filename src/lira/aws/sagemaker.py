"""The classical NLP models on SageMaker: training job entry point, inference handlers, model registry, and a
canary (blue/green) endpoint update that rolls back on CloudWatch alarms.

Lifecycle: a training job trains on the client's review history -> the model package is registered as
PendingManualApproval with its validation metrics -> it is approved only if the evaluation gate passed for that
exact artifact -> the endpoint shifts 10% of traffic to it, waits, then the rest; any alarm rolls it back.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- training job (SageMaker script mode)


def train_entry(argv: list[str] | None = None) -> int:
    """Entry point inside the training container: SM_CHANNEL_TRAIN in, SM_MODEL_DIR out, metrics on stdout."""
    from lira.nlp.models import train

    train_dir = Path(os.environ.get("SM_CHANNEL_TRAIN", "/opt/ml/input/data/train"))
    model_dir = Path(os.environ.get("SM_MODEL_DIR", "/opt/ml/model"))
    rows = [json.loads(x) for x in (train_dir / "decisions.jsonl").read_text().splitlines() if x.strip()]
    appeals_path = train_dir / "appeals.jsonl"
    appeals = [json.loads(x) for x in appeals_path.read_text().splitlines() if x.strip()] if appeals_path.exists() else None
    months = sorted({r["date"][:7] for r in rows})
    model = train(rows, appeals, validation_from=months[-2] + "-01" if len(months) > 3 else None)
    model.save(model_dir)
    for q, m in model.card["metrics"].items():
        if isinstance(m, dict):
            for k, v in m.items():
                if isinstance(v, int | float):
                    print(f"metric: {q}.{k}={v}")  # matched by the job's MetricDefinitions regex
    return 0


METRIC_DEFINITIONS = [
    {"Name": n, "Regex": rf"metric: {n.replace('.', r'\.')}=([0-9.]+)"}
    for n in (
        "block_billing.auc_vs_reviewer_labels",
        "vague_description.auc_vs_reviewer_labels",
        "clerical_work.auc_vs_reviewer_labels",
        "task_code.accuracy_vs_firm_codes",
    )
]

# ---------------------------------------------------------------- inference (SageMaker scikit-learn container handlers)


def model_fn(model_dir: str) -> Any:
    from lira.nlp.models import NlpModels

    return NlpModels.load(Path(model_dir))


def input_fn(body: bytes | str, content_type: str) -> list[dict[str, Any]]:
    if content_type != "application/json":
        raise ValueError(f"unsupported content type {content_type}")
    data = json.loads(body)
    rows = data["lines"] if isinstance(data, dict) else data
    if not isinstance(rows, list) or len(rows) > 500:
        raise ValueError("expected a list of at most 500 lines")
    return rows


def predict_fn(rows: list[dict[str, Any]], model: Any) -> dict[str, Any]:
    probs = model.judgement_probs(rows)
    codes, conf = model.task_codes([r["description"] for r in rows])
    return {
        "model": model.version,
        "lines": [
            {
                "p": {q: round(float(v[i]), 4) for q, v in probs.items()},
                "task_code": codes[i],
                "task_code_confidence": round(float(conf[i]), 4),
            }
            for i in range(len(rows))
        ],
    }


def output_fn(prediction: dict[str, Any], accept: str) -> str:
    return json.dumps(prediction)


# ---------------------------------------------------------------- training job, registry and deployment requests


def training_job_request(
    name: str,
    role_arn: str,
    image_uri: str,
    train_s3: str,
    output_s3: str,
    source_s3: str,
    kms_key: str,
    subnets: list[str],
    security_groups: list[str],
) -> dict[str, Any]:
    return {
        "TrainingJobName": name,
        "RoleArn": role_arn,
        "AlgorithmSpecification": {"TrainingImage": image_uri, "TrainingInputMode": "File", "MetricDefinitions": METRIC_DEFINITIONS},
        "HyperParameters": {"sagemaker_program": "lira_train.py", "sagemaker_submit_directory": source_s3},
        "InputDataConfig": [
            {
                "ChannelName": "train",
                "DataSource": {"S3DataSource": {"S3DataType": "S3Prefix", "S3Uri": train_s3, "S3DataDistributionType": "FullyReplicated"}},
            }
        ],
        "OutputDataConfig": {"S3OutputPath": output_s3, "KmsKeyId": kms_key},
        "ResourceConfig": {"InstanceType": "ml.m5.xlarge", "InstanceCount": 1, "VolumeSizeInGB": 20, "VolumeKmsKeyId": kms_key},
        "StoppingCondition": {"MaxRuntimeInSeconds": 3600},
        "VpcConfig": {"Subnets": subnets, "SecurityGroupIds": security_groups},
        "EnableNetworkIsolation": True,
        "EnableInterContainerTrafficEncryption": True,
        "Tags": [{"Key": "app", "Value": "legal-invoice-review"}],
    }


def register_request(group: str, image_uri: str, model_data_url: str, metrics_s3: str, version: str) -> dict[str, Any]:
    return {
        "ModelPackageGroupName": group,
        "ModelPackageDescription": f"classical NLP models {version}",
        "ModelApprovalStatus": "PendingManualApproval",
        "InferenceSpecification": {
            "Containers": [{"Image": image_uri, "ModelDataUrl": model_data_url, "Environment": {"SAGEMAKER_PROGRAM": "inference.py"}}],
            "SupportedContentTypes": ["application/json"],
            "SupportedResponseMIMETypes": ["application/json"],
            "SupportedRealtimeInferenceInstanceTypes": ["ml.m5.large", "ml.c5.large"],
        },
        "ModelMetrics": {"ModelQuality": {"Statistics": {"ContentType": "application/json", "S3Uri": metrics_s3}}},
        "CustomerMetadataProperties": {"nlp_version": version},
    }


def approval_request(package_arn: str, gate_passed: bool, report_sha: str) -> dict[str, Any]:
    """Approve only a package whose exact artifact passed the release gate; otherwise reject it with the reason."""
    return {
        "ModelPackageArn": package_arn,
        "ModelApprovalStatus": "Approved" if gate_passed else "Rejected",
        "ApprovalDescription": f"release gate {'passed' if gate_passed else 'failed'} (report sha256 {report_sha[:16]})",
    }


def canary_update_request(
    endpoint: str, endpoint_config: str, alarms: list[str], canary_percent: int = 10, wait_seconds: int = 600
) -> dict[str, Any]:
    return {
        "EndpointName": endpoint,
        "EndpointConfigName": endpoint_config,
        "DeploymentConfig": {
            "BlueGreenUpdatePolicy": {
                "TrafficRoutingConfiguration": {
                    "Type": "CANARY",
                    "CanarySize": {"Type": "CAPACITY_PERCENT", "Value": canary_percent},
                    "WaitIntervalInSeconds": wait_seconds,
                },
                "TerminationWaitInSeconds": 300,
                "MaximumExecutionTimeoutInSeconds": 3600,
            },
            "AutoRollbackConfiguration": {"Alarms": [{"AlarmName": a} for a in alarms]},
        },
    }


class EndpointScorer:
    """Score lines on the SageMaker endpoint instead of in process (config: nlp serving = endpoint)."""

    def __init__(self, runtime: Any, endpoint: str):
        self.runtime, self.endpoint = runtime, endpoint

    def score(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        r = self.runtime.invoke_endpoint(
            EndpointName=self.endpoint, ContentType="application/json", Accept="application/json", Body=json.dumps({"lines": rows})
        )
        return json.loads(r["Body"].read())


if __name__ == "__main__":
    sys.exit(train_entry())

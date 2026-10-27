"""The infrastructure, synthesised and checked: cdk-nag AwsSolutions passes, data is encrypted and private, model
traffic stays in the EU, the knowledge base and guardrail are configured as the application expects."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytestmark = pytest.mark.infra

aws_cdk = pytest.importorskip("aws_cdk")
from aws_cdk import assertions  # noqa: E402
from cdk_nag import AwsSolutionsChecks  # noqa: E402
from lira_infra.stage import build  # noqa: E402


@pytest.fixture(scope="module")
def synth():
    app = aws_cdk.App()
    stacks = build(app, "test", "abc123", "owner/legal-invoice-review-agents", alarm_email="ops@example.com")
    aws_cdk.Aspects.of(app).add(AwsSolutionsChecks())
    asm = app.synth()
    templates = {k: assertions.Template.from_stack(s) for k, s in stacks.items()}
    messages = {s.stack_name: [m for m in s.messages if m.level.name == "ERROR"] for s in asm.stacks}
    return templates, messages


def test_cdk_nag_aws_solutions_has_no_errors(synth):
    _, messages = synth
    problems = {name: [m.id for m in ms] for name, ms in messages.items() if ms}
    assert problems == {}


def test_buckets_are_private_encrypted_and_tls_only(synth):
    t = synth[0]["foundation"]
    buckets = t.find_resources("AWS::S3::Bucket")
    assert len(buckets) == 4
    for b in buckets.values():
        p = b["Properties"]
        assert p["PublicAccessBlockConfiguration"] == {
            "BlockPublicAcls": True,
            "BlockPublicPolicy": True,
            "IgnorePublicAcls": True,
            "RestrictPublicBuckets": True,
        }
        assert "BucketEncryption" in p
    policies = t.find_resources("AWS::S3::BucketPolicy")
    denies = [s for p in policies.values() for s in p["Properties"]["PolicyDocument"]["Statement"] if s["Effect"] == "Deny"]
    assert len(denies) >= 4 and all(s["Condition"]["Bool"]["aws:SecureTransport"] == "false" for s in denies)


def test_database_is_encrypted_private_and_uses_iam_auth(synth):
    t = synth[0]["foundation"]
    t.has_resource_properties(
        "AWS::RDS::DBCluster",
        {
            "StorageEncrypted": True,
            "EnableIAMDatabaseAuthentication": True,
            "DeletionProtection": True,
            "Engine": "aurora-postgresql",
            "ServerlessV2ScalingConfiguration": {"MinCapacity": 0.5, "MaxCapacity": 8},
        },
    )


def test_model_traffic_uses_private_endpoints(synth):
    t = synth[0]["foundation"]
    services = json.dumps(t.find_resources("AWS::EC2::VPCEndpoint"))
    for svc in ("bedrock-runtime", "bedrock-agent-runtime", "sagemaker.runtime", "secretsmanager", "sqs"):
        assert svc in services


def test_review_queue_has_encrypted_dead_letter_queue(synth):
    t = synth[0]["foundation"]
    t.has_resource_properties("AWS::SQS::Queue", {"RedrivePolicy": {"maxReceiveCount": 3}, "KmsMasterKeyId": assertions.Match.any_value()})


def test_guardrail_blocks_prompt_attacks_and_checks_grounding(synth):
    t = synth[0]["ai"]
    g = next(iter(t.find_resources("AWS::Bedrock::Guardrail").values()))["Properties"]
    filters = {f["Type"]: f for f in g["ContentPolicyConfig"]["FiltersConfig"]}
    assert filters["PROMPT_ATTACK"]["InputStrength"] == "HIGH"
    grounding = {f["Type"]: f["Threshold"] for f in g["ContextualGroundingPolicyConfig"]["FiltersConfig"]}
    assert grounding["GROUNDING"] >= 0.75
    pii = {p["Type"] for p in g["SensitiveInformationPolicyConfig"]["PiiEntitiesConfig"]}
    assert {"EMAIL", "INTERNATIONAL_BANK_ACCOUNT_NUMBER"} <= pii


def test_knowledge_base_on_private_vector_collection_with_clause_chunks(synth):
    t = synth[0]["ai"]
    t.has_resource_properties("AWS::OpenSearchServerless::Collection", {"Type": "VECTORSEARCH"})
    net = next(
        json.loads(p["Properties"]["Policy"])
        for p in t.find_resources("AWS::OpenSearchServerless::SecurityPolicy").values()
        if p["Properties"]["Type"] == "network"
    )
    assert net[0]["AllowFromPublic"] is False
    t.has_resource_properties("AWS::Bedrock::KnowledgeBase", {"StorageConfiguration": {"Type": "OPENSEARCH_SERVERLESS"}})
    t.has_resource_properties(
        "AWS::Bedrock::DataSource", {"VectorIngestionConfiguration": {"ChunkingConfiguration": {"ChunkingStrategy": "NONE"}}}
    )
    t.has_resource_properties("AWS::SageMaker::ModelPackageGroup", {"ModelPackageGroupName": "lira-test-nlp"})


def test_task_role_can_only_invoke_eu_models(synth):
    t = synth[0]["service"]
    statements = [
        s
        for p in t.find_resources("AWS::IAM::Policy").values()
        for s in p["Properties"]["PolicyDocument"]["Statement"]
        if "bedrock:InvokeModel" in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])
    ]
    resources = json.dumps([s["Resource"] for s in statements])
    assert "*" not in [
        r for s in statements for r in (s["Resource"] if isinstance(s["Resource"], list) else [s["Resource"]]) if isinstance(r, str)
    ]
    assert "us." not in resources and "anthropic.claude" in resources and "eu." in resources


def test_load_balancer_is_internal_and_services_roll_back(synth):
    t = synth[0]["service"]
    t.has_resource_properties("AWS::ElasticLoadBalancingV2::LoadBalancer", {"Scheme": "internal"})
    services = t.find_resources("AWS::ECS::Service")
    assert len(services) == 2
    for s in services.values():
        assert s["Properties"]["DeploymentConfiguration"]["DeploymentCircuitBreaker"] == {"Enable": True, "Rollback": True}


def test_alarms_cover_dead_letters_drift_and_degraded_model(synth):
    t = synth[0]["service"]
    descs = " ".join(a["Properties"].get("AlarmDescription", "") for a in t.find_resources("AWS::CloudWatch::Alarm").values())
    for word in ("failed review three times", "drifted", "without the model", "failing verification"):
        assert word in descs


def test_github_deploy_role_is_scoped_to_one_repo_environment(synth):
    t = synth[0]["service"]
    roles = [r for r in t.find_resources("AWS::IAM::Role").values() if r["Properties"].get("RoleName") == "lira-test-deploy"]
    cond = roles[0]["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]["Condition"]
    assert cond["StringLike"]["token.actions.githubusercontent.com:sub"] == "repo:owner/legal-invoice-review-agents:environment:test"

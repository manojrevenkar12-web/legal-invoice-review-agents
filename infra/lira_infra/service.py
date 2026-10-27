"""The running system: the API and the review worker on ECS Fargate, the intake Lambda, alarms and a dashboard,
and the role GitHub Actions deploys with (OIDC, no stored keys).

One container image serves three entry points: `lira serve` (API), `lira worker` (SQS consumer and job runner)
and the Lambda handler `lira.aws.ingest.handler`.
"""

from __future__ import annotations

from aws_cdk import Duration, Stack
from aws_cdk import aws_applicationautoscaling as appscaling
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from cdk_nag import NagSuppressions
from constructs import Construct

from lira_infra.ai import AiStack
from lira_infra.foundation import FoundationStack

NAMESPACE = "LegalInvoiceReview"  # CloudWatch EMF namespace written by lira.obs.metrics
EU_MODELS = ["anthropic.claude-sonnet-4-5-20250929-v1:0", "anthropic.claude-haiku-4-5-20251001-v1:0"]


class ServiceStack(Stack):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        env_name: str,
        base: FoundationStack,
        ai: AiStack,
        image_tag: str,
        github_repo: str,
        alarm_email: str | None = None,
        **kw: object,
    ) -> None:
        super().__init__(scope, id, **kw)  # type: ignore[arg-type]
        repo = ecr.Repository.from_repository_name(self, "Repo", f"lira-{env_name}")
        image = ecs.ContainerImage.from_ecr_repository(repo, tag=image_tag)
        cluster = ecs.Cluster(self, "Cluster", vpc=base.vpc, container_insights_v2=ecs.ContainerInsights.ENHANCED)
        logs_group = logs.LogGroup(self, "Logs", retention=logs.RetentionDays.ONE_YEAR, encryption_key=base.key)
        env = {
            "LIRA_MODEL": "live",
            "LIRA_EMBEDDER": "titan",
            "AWS_REGION": self.region,
            "LIRA_REVIEW_QUEUE_URL": base.queue.queue_url,
            "LIRA_GUARDRAIL_ID": ai.guardrail.attr_guardrail_id,
            "LIRA_GUARDRAIL_VERSION": ai.guardrail_version.attr_version,
            "LIRA_KNOWLEDGE_BASE_ID": ai.knowledge_base.attr_knowledge_base_id,
            "LIRA_DB_HOST": base.db.cluster_endpoint.hostname,
            "LIRA_DB_IAM_AUTH": "1",
            "LIRA_INBOX_BUCKET": base.inbox.bucket_name,
        }

        # ---- task role shared by the API and the worker (least privilege per action)
        role = iam.Role(self, "TaskRole", role_name=f"lira-{env_name}-worker", assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"))
        model_arns = [
            f"arn:aws:bedrock:{r}::foundation-model/{m}"
            for m in EU_MODELS
            for r in ("eu-central-1", "eu-west-1", "eu-west-3", "eu-north-1")
        ]
        profile_arns = [f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/eu.{m}" for m in EU_MODELS]
        role.add_to_policy(
            iam.PolicyStatement(
                sid="InvokeEuModels",
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=model_arns + profile_arns + [f"arn:aws:bedrock:{self.region}::foundation-model/amazon.titan-embed-text-v2:0"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(sid="Guardrail", actions=["bedrock:ApplyGuardrail"], resources=[ai.guardrail.attr_guardrail_arn])
        )
        role.add_to_policy(
            iam.PolicyStatement(sid="KnowledgeBase", actions=["bedrock:Retrieve"], resources=[ai.knowledge_base.attr_knowledge_base_arn])
        )
        role.add_to_policy(iam.PolicyStatement(sid="Rerank", actions=["bedrock:Rerank"], resources=["*"]))
        role.add_to_policy(
            iam.PolicyStatement(
                sid="DbConnect",
                actions=["rds-db:connect"],
                resources=[f"arn:aws:rds-db:{self.region}:{self.account}:dbuser:{base.db.cluster_resource_identifier}/lira_app"],
            )
        )
        base.queue.grant_consume_messages(role)
        base.inbox.grant_read(role)
        base.knowledge.grant_read_write(role, "approved/*")
        base.artifacts.grant_read(role, "nlp/*")
        base.key.grant_encrypt_decrypt(role)
        base.db.connections.allow_default_port_from(ec2.Peer.ipv4(base.vpc.vpc_cidr_block), "app tasks")

        def task(name: str, command: list[str], cpu: int, memory: int, port: int | None = None) -> ecs.FargateTaskDefinition:
            td = ecs.FargateTaskDefinition(
                self,
                f"{name}Task",
                cpu=cpu,
                memory_limit_mib=memory,
                task_role=role,
                runtime_platform=ecs.RuntimePlatform(
                    cpu_architecture=ecs.CpuArchitecture.X86_64, operating_system_family=ecs.OperatingSystemFamily.LINUX
                ),
            )
            c = td.add_container(
                name,
                image=image,
                command=command,
                environment=env,
                readonly_root_filesystem=True,
                logging=ecs.LogDrivers.aws_logs(stream_prefix=name.lower(), log_group=logs_group),
                health_check=ecs.HealthCheck(command=["CMD-SHELL", "python -c 'import lira' || exit 1"]) if port is None else None,
            )
            td.add_volume(name="tmp")
            c.add_mount_points(ecs.MountPoint(container_path="/tmp", source_volume="tmp", read_only=False))
            if port:
                c.add_port_mappings(ecs.PortMapping(container_port=port))
            return td

        app_sg = ec2.SecurityGroup(self, "AppSg", vpc=base.vpc, description="API and worker tasks")
        api_td = task("Api", ["lira", "serve", "--port", "8080"], 1024, 2048, 8080)
        api = ecs.FargateService(
            self,
            "ApiService",
            cluster=cluster,
            task_definition=api_td,
            desired_count=2,
            security_groups=[app_sg],
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            circuit_breaker=ecs.DeploymentCircuitBreaker(enable=True, rollback=True),
            min_healthy_percent=100,
        )
        alb_sg = ec2.SecurityGroup(self, "AlbSg", vpc=base.vpc, description="Internal load balancer", allow_all_outbound=False)
        alb_sg.add_ingress_rule(ec2.Peer.ipv4(base.vpc.vpc_cidr_block), ec2.Port.tcp(80), "from inside the VPC (corporate network via TGW)")
        alb_sg.add_egress_rule(app_sg, ec2.Port.tcp(8080), "to the API tasks")
        app_sg.add_ingress_rule(alb_sg, ec2.Port.tcp(8080), "from the load balancer")
        alb = elbv2.ApplicationLoadBalancer(
            self,
            "Alb",
            vpc=base.vpc,
            internet_facing=False,
            security_group=alb_sg,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            drop_invalid_header_fields=True,
        )
        alb.log_access_logs(base.access_logs, prefix="alb")
        listener = alb.add_listener("Http", port=80, open=False)
        target = listener.add_targets(
            "Api",
            port=8080,
            targets=[api],
            health_check=elbv2.HealthCheck(path="/readyz", healthy_http_codes="200"),
            deregistration_delay=Duration.seconds(30),
        )
        scaling = api.auto_scale_task_count(min_capacity=2, max_capacity=8)
        scaling.scale_on_cpu_utilization("Cpu", target_utilization_percent=60)

        worker_td = task("Worker", ["lira", "worker"], 2048, 4096)
        worker = ecs.FargateService(
            self,
            "WorkerService",
            cluster=cluster,
            task_definition=worker_td,
            desired_count=1,
            security_groups=[app_sg],
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            circuit_breaker=ecs.DeploymentCircuitBreaker(enable=True, rollback=True),
            min_healthy_percent=100,
        )
        wscale = worker.auto_scale_task_count(min_capacity=1, max_capacity=6)
        wscale.scale_on_metric(
            "QueueDepth",
            metric=base.queue.metric_approximate_number_of_messages_visible(period=Duration.minutes(1)),
            scaling_steps=[{"upper": 0, "change": -1}, {"lower": 20, "change": +1}, {"lower": 100, "change": +3}],
            adjustment_type=appscaling.AdjustmentType.CHANGE_IN_CAPACITY,
        )

        # ---- intake Lambda: S3 ObjectCreated -> format check -> SQS
        fn_role = iam.Role(self, "IngestRole", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"))
        fn_role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaVPCAccessExecutionRole"))
        base.inbox.grant_read_write(fn_role)
        base.inbox.grant_delete(fn_role)
        base.queue.grant_send_messages(fn_role)
        base.key.grant_encrypt_decrypt(fn_role)
        fn = lambda_.DockerImageFunction(
            self,
            "Ingest",
            code=lambda_.DockerImageCode.from_ecr(
                repo, tag_or_digest=image_tag, cmd=["lira.aws.ingest.handler"], entrypoint=["python", "-m", "awslambdaric"]
            ),
            role=fn_role,
            timeout=Duration.seconds(60),
            memory_size=512,
            vpc=base.vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[app_sg],
            environment={"LIRA_REVIEW_QUEUE_URL": base.queue.queue_url},
            reserved_concurrent_executions=20,
            tracing=lambda_.Tracing.ACTIVE,
            log_group=logs.LogGroup(self, "IngestLogs", retention=logs.RetentionDays.ONE_YEAR, encryption_key=base.key),
        )
        # S3 -> EventBridge -> Lambda keeps the dependency one-way (this stack on the foundation stack)
        events.Rule(
            self,
            "InvoiceArrived",
            description="A LEDES file arrived in the inbox",
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={"bucket": {"name": [base.inbox.bucket_name]}, "object": {"key": [{"suffix": ".ledes"}]}},
            ),
            targets=[targets.LambdaFunction(fn, retry_attempts=4, max_event_age=Duration.hours(2))],
        )

        # ---- alarms and dashboard
        topic = sns.Topic(self, "Alarms", master_key=base.key, display_name="Legal invoice review alarms")
        if alarm_email:
            sns.Subscription(self, "AlarmMail", topic=topic, protocol=sns.SubscriptionProtocol.EMAIL, endpoint=alarm_email)

        def emf(name: str, stat: str = "Sum", **dims: str) -> cw.Metric:
            return cw.Metric(namespace=NAMESPACE, metric_name=name, dimensions_map=dims or None, statistic=stat, period=Duration.minutes(5))

        alarms = {
            "DeadLetters": cw.Alarm(
                self,
                "DeadLetters",
                metric=base.dlq.metric_approximate_number_of_messages_visible(),
                threshold=0,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                evaluation_periods=1,
                alarm_description="Invoices that failed review three times: see docs/RUNBOOK.md#dead-letters",
            ),
            "Api5xx": cw.Alarm(
                self,
                "Api5xx",
                metric=target.metrics.http_code_target(elbv2.HttpCodeTarget.TARGET_5XX_COUNT),
                threshold=5,
                evaluation_periods=2,
                alarm_description="API errors",
            ),
            "ModelDegraded": cw.Alarm(
                self,
                "ModelDegraded",
                metric=emf("batches", node="review_batch", outcome="degraded"),
                threshold=10,
                evaluation_periods=3,
                alarm_description="Reviews running without the model: judgement goes to people only",
            ),
            "Hallucinations": cw.Alarm(
                self,
                "Hallucinations",
                metric=emf("dropped", node="adjudicate", outcome="hallucination"),
                threshold=25,
                evaluation_periods=3,
                alarm_description="Model outputs failing verification above normal: check prompt and model",
            ),
            "Drift": cw.Alarm(
                self,
                "Drift",
                metric=emf("drift_alert", "Maximum", check="narrative_drift"),
                threshold=0,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                evaluation_periods=1,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
                alarm_description="A firm's narratives drifted from the reference window: docs/RUNBOOK.md#drift",
            ),
        }
        for a in alarms.values():
            a.add_alarm_action(cw_actions.SnsAction(topic))
        dash = cw.Dashboard(self, "Dashboard", dashboard_name=f"lira-{env_name}")
        dash.add_widgets(
            cw.GraphWidget(
                title="Model calls by outcome",
                left=[
                    emf("calls", task="line_review", model=m, outcome=o)
                    for m in ("haiku-eu", "sonnet-eu")
                    for o in ("ok", "error", "invalid")
                ],
            ),
            cw.GraphWidget(
                title="Model cost (USD)",
                left=[emf("cost_usd", task=t, model="haiku-eu", outcome="ok") for t in ("line_review", "qa_answer")],
            ),
            cw.GraphWidget(
                title="Review queue",
                left=[base.queue.metric_approximate_number_of_messages_visible(), base.dlq.metric_approximate_number_of_messages_visible()],
            ),
            cw.AlarmStatusWidget(title="Alarms", alarms=list(alarms.values())),
        )

        # ---- CI/CD: GitHub Actions deploys through OIDC (no long-lived keys)
        provider = iam.OpenIdConnectProvider(
            self, "GitHub", url="https://token.actions.githubusercontent.com", client_ids=["sts.amazonaws.com"]
        )
        deploy = iam.Role(
            self,
            "DeployRole",
            role_name=f"lira-{env_name}-deploy",
            max_session_duration=Duration.hours(1),
            assumed_by=iam.WebIdentityPrincipal(
                provider.open_id_connect_provider_arn,
                conditions={
                    "StringEquals": {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com"},
                    "StringLike": {"token.actions.githubusercontent.com:sub": f"repo:{github_repo}:environment:{env_name}"},
                },
            ),
        )
        deploy.add_to_policy(
            iam.PolicyStatement(sid="AssumeCdkRoles", actions=["sts:AssumeRole"], resources=[f"arn:aws:iam::{self.account}:role/cdk-*"])
        )
        repo.grant_pull_push(deploy)

        NagSuppressions.add_stack_suppressions(
            self,
            [
                {"id": "AwsSolutions-IAM4", "reason": "AWSLambdaVPCAccessExecutionRole is the AWS-maintained minimum for a VPC Lambda."},
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": "Wildcards are limited to object prefixes, KMS data-key actions, the CDK bootstrap roles "
                    "and bedrock:Rerank (which has no resource-level permissions).",
                },
                {
                    "id": "AwsSolutions-ECS2",
                    "reason": "Environment variables hold identifiers only; credentials come from IAM roles and IAM DB auth.",
                },
                {
                    "id": "AwsSolutions-ELB2",
                    "reason": "Access logs are enabled (log_access_logs); the rule does not see the L2 call through the token.",
                },
                {"id": "AwsSolutions-EC23", "reason": "The load balancer is internal and accepts only the VPC CIDR."},
                {
                    "id": "AwsSolutions-L1",
                    "reason": "The function runs the application's own container image; the runtime is pinned in the Dockerfile.",
                },
                {"id": "AwsSolutions-SNS3", "reason": "Subscribers are e-mail and chat integrations owned by the operations team."},
            ],
        )
        self.alb = alb
        self.api_target = target

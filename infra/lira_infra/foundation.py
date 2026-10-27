"""Network and data: VPC with private subnets and interface endpoints (model traffic never crosses the internet),
KMS, S3 (inbox, artifacts, knowledge base), Aurora PostgreSQL Serverless v2, SQS with a dead-letter queue."""

from __future__ import annotations

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_rds as rds
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sqs as sqs
from cdk_nag import NagSuppressions
from constructs import Construct


class FoundationStack(Stack):
    def __init__(self, scope: Construct, id: str, *, env_name: str, **kw: object) -> None:
        super().__init__(scope, id, **kw)  # type: ignore[arg-type]
        self.key = kms.Key(
            self,
            "Key",
            alias=f"alias/lira-{env_name}",
            enable_key_rotation=True,
            description="Legal invoice review: data at rest",
            removal_policy=RemovalPolicy.RETAIN,
        )

        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            ip_addresses=ec2.IpAddresses.cidr("10.40.0.0/16"),
            max_azs=2,
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24),
                ec2.SubnetConfiguration(name="app", subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS, cidr_mask=22),
                ec2.SubnetConfiguration(name="data", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24),
            ],
        )
        flow_logs = logs.LogGroup(self, "FlowLogs", retention=logs.RetentionDays.ONE_YEAR, encryption_key=self.key)
        self.vpc.add_flow_log("Flow", destination=ec2.FlowLogDestination.to_cloud_watch_logs(flow_logs))
        self.endpoint_sg = ec2.SecurityGroup(
            self, "EndpointSg", vpc=self.vpc, allow_all_outbound=False, description="Interface endpoints: HTTPS from inside the VPC"
        )
        self.endpoint_sg.add_ingress_rule(ec2.Peer.ipv4(self.vpc.vpc_cidr_block), ec2.Port.tcp(443), "HTTPS from the VPC")
        for name, svc in {
            "BedrockRuntime": ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME,
            "BedrockAgentRuntime": ec2.InterfaceVpcEndpointAwsService.BEDROCK_AGENT_RUNTIME,
            "SageMakerRuntime": ec2.InterfaceVpcEndpointAwsService.SAGEMAKER_RUNTIME,
            "SecretsManager": ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
            "Sqs": ec2.InterfaceVpcEndpointAwsService.SQS,
            "Logs": ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
            "Ecr": ec2.InterfaceVpcEndpointAwsService.ECR,
            "EcrDocker": ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
            "Sts": ec2.InterfaceVpcEndpointAwsService.STS,
        }.items():
            self.vpc.add_interface_endpoint(
                name,
                service=svc,
                security_groups=[self.endpoint_sg],
                subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            )
        self.vpc.add_gateway_endpoint("S3", service=ec2.GatewayVpcEndpointAwsService.S3)

        self.access_logs = s3.Bucket(
            self,
            "AccessLogs",
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_PREFERRED,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(400))],
            removal_policy=RemovalPolicy.RETAIN,
        )

        def bucket(name: str, versioned: bool = True, events: bool = False) -> s3.Bucket:
            return s3.Bucket(
                self,
                name,
                encryption=s3.BucketEncryption.KMS,
                encryption_key=self.key,
                bucket_key_enabled=True,
                event_bridge_enabled=events,
                enforce_ssl=True,
                versioned=versioned,
                block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
                server_access_logs_bucket=self.access_logs,
                server_access_logs_prefix=f"{name.lower()}/",
                removal_policy=RemovalPolicy.RETAIN,
            )

        self.inbox = bucket("Inbox", events=True)  # <tenant>/<firm>/<invoice>.ledes; rejected/ for files that fail the format check
        self.artifacts = bucket("Artifacts")  # training data, model artifacts, evaluation reports
        self.knowledge = bucket("Knowledge")  # approved guideline clauses with metadata, the knowledge base data source

        self.db_sg = ec2.SecurityGroup(self, "DbSg", vpc=self.vpc, allow_all_outbound=False, description="Aurora PostgreSQL")
        self.db = rds.DatabaseCluster(
            self,
            "Db",
            engine=rds.DatabaseClusterEngine.aurora_postgres(version=rds.AuroraPostgresEngineVersion.VER_16_9),
            writer=rds.ClusterInstance.serverless_v2("writer"),
            readers=[rds.ClusterInstance.serverless_v2("reader", scale_with_writer=True)],
            serverless_v2_min_capacity=0.5,
            serverless_v2_max_capacity=8,
            vpc=self.vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            security_groups=[self.db_sg],
            credentials=rds.Credentials.from_generated_secret("lira_admin", encryption_key=self.key),
            default_database_name="lira",
            storage_encrypted=True,
            storage_encryption_key=self.key,
            iam_authentication=True,
            deletion_protection=True,
            backup=rds.BackupProps(retention=Duration.days(14)),
            cloudwatch_logs_exports=["postgresql"],
            cloudwatch_logs_retention=logs.RetentionDays.ONE_YEAR,
            removal_policy=RemovalPolicy.SNAPSHOT,
        )
        self.dlq = sqs.Queue(
            self,
            "ReviewDlq",
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=self.key,
            enforce_ssl=True,
            retention_period=Duration.days(14),
        )
        self.queue = sqs.Queue(
            self,
            "ReviewQueue",
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=self.key,
            enforce_ssl=True,
            visibility_timeout=Duration.minutes(15),
            dead_letter_queue=sqs.DeadLetterQueue(queue=self.dlq, max_receive_count=3),
        )

        NagSuppressions.add_resource_suppressions(
            self.db,
            [
                {
                    "id": "AwsSolutions-SMG4",
                    "reason": "Credentials rotate through the Secrets Manager rotation set up by the platform team's "
                    "account baseline; the application connects with IAM authentication.",
                },
                {
                    "id": "AwsSolutions-RDS6",
                    "reason": "IAM authentication is enabled; the generated admin secret is used only for migrations.",
                },
            ],
            apply_to_children=True,
        )
        NagSuppressions.add_resource_suppressions(self.dlq, [{"id": "AwsSolutions-SQS3", "reason": "This is the dead-letter queue."}])
        NagSuppressions.add_resource_suppressions(
            self.access_logs, [{"id": "AwsSolutions-S1", "reason": "This bucket receives the access logs of the other buckets."}]
        )

    def suppress_cdk_helpers(self) -> None:
        """CDK's own singleton functions (S3 EventBridge switch, log retention) use the AWS basic execution policy."""
        for path in (
            f"/{self.stack_name}/BucketNotificationsHandler050a0587b7544547bf325f094a3db834",
            f"/{self.stack_name}/LogRetentionaae0aa3c5b4d4f87b02d85b201efdd8a",
        ):
            NagSuppressions.add_resource_suppressions_by_path(
                self,
                path,
                [
                    {"id": "AwsSolutions-IAM4", "reason": "CDK-provided helper function; basic execution role only."},
                    {"id": "AwsSolutions-IAM5", "reason": "CDK-provided helper function (log retention needs logs:* on its log groups)."},
                ],
                apply_to_children=True,
            )

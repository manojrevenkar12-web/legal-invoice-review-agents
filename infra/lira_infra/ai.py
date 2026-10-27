"""Model-facing infrastructure: OpenSearch Serverless vector collection and index, Bedrock Knowledge Base over the
approved guideline clauses, Bedrock Guardrail (prompt attacks, PII, contextual grounding), SageMaker model registry."""

from __future__ import annotations

import json

from aws_cdk import Stack
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_opensearchserverless as oss
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sagemaker as sagemaker
from cdk_nag import NagSuppressions
from constructs import Construct

EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"
INDEX = "guideline-clauses"


class AiStack(Stack):
    def __init__(
        self, scope: Construct, id: str, *, env_name: str, key: kms.IKey, knowledge: s3.IBucket, artifacts: s3.IBucket, **kw: object
    ) -> None:
        super().__init__(scope, id, **kw)  # type: ignore[arg-type]
        collection = f"lira-{env_name}-clauses"

        # ---- OpenSearch Serverless (vector search)
        enc = oss.CfnSecurityPolicy(
            self,
            "Encryption",
            name=f"lira-{env_name}-enc",
            type="encryption",
            policy=json.dumps({"Rules": [{"ResourceType": "collection", "Resource": [f"collection/{collection}"]}], "KmsARN": key.key_arn}),
        )
        net = oss.CfnSecurityPolicy(
            self,
            "Network",
            name=f"lira-{env_name}-net",
            type="network",
            policy=json.dumps(
                [
                    {
                        "Rules": [{"ResourceType": "collection", "Resource": [f"collection/{collection}"]}],
                        "AllowFromPublic": False,
                        "SourceServices": ["bedrock.amazonaws.com"],
                    }
                ]
            ),
        )
        self.collection = oss.CfnCollection(
            self,
            "Collection",
            name=collection,
            type="VECTORSEARCH",
            standby_replicas="ENABLED",
            description="Approved guideline clauses (hybrid BM25 + k-NN)",
        )
        self.collection.add_resource_dependency(enc)
        self.collection.add_resource_dependency(net)

        self.kb_role = iam.Role(
            self,
            "KnowledgeBaseRole",
            assumed_by=iam.ServicePrincipal("bedrock.amazonaws.com"),
            description="Bedrock Knowledge Base: read clauses, embed, write the vector index",
        )
        self.kb_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"], resources=[f"arn:aws:bedrock:{self.region}::foundation-model/{EMBEDDING_MODEL}"]
            )
        )
        self.kb_role.add_to_policy(iam.PolicyStatement(actions=["aoss:APIAccessAll"], resources=[self.collection.attr_arn]))
        knowledge.grant_read(self.kb_role)
        key.grant_decrypt(self.kb_role)

        self.reader_role_arns: list[str] = []
        self.access = oss.CfnAccessPolicy(
            self,
            "Access",
            name=f"lira-{env_name}-access",
            type="data",
            policy=json.dumps(
                [
                    {
                        "Rules": [
                            {
                                "ResourceType": "index",
                                "Resource": [f"index/{collection}/*"],
                                "Permission": [
                                    "aoss:CreateIndex",
                                    "aoss:DescribeIndex",
                                    "aoss:ReadDocument",
                                    "aoss:WriteDocument",
                                    "aoss:UpdateIndex",
                                ],
                            },
                            {
                                "ResourceType": "collection",
                                "Resource": [f"collection/{collection}"],
                                "Permission": ["aoss:DescribeCollectionItems"],
                            },
                        ],
                        "Principal": [self.kb_role.role_arn, f"arn:aws:iam::{self.account}:role/lira-{env_name}-worker"],
                    }
                ]
            ),
        )

        self.index = oss.CfnIndex(
            self,
            "Index",
            collection_endpoint=self.collection.attr_collection_endpoint,
            index_name=INDEX,
            settings=oss.CfnIndex.IndexSettingsProperty(index=oss.CfnIndex.IndexProperty(knn=True, knn_algo_param_ef_search=256)),
            mappings=oss.CfnIndex.MappingsProperty(
                properties={
                    "embedding": oss.CfnIndex.PropertyMappingProperty(
                        type="knn_vector",
                        dimension=1024,
                        method=oss.CfnIndex.MethodProperty(name="hnsw", engine="faiss", space_type="innerproduct"),
                    ),
                    "text": oss.CfnIndex.PropertyMappingProperty(type="text", index=True),
                    "metadata": oss.CfnIndex.PropertyMappingProperty(type="text", index=False),
                    "tenant": oss.CfnIndex.PropertyMappingProperty(type="keyword", index=True),
                    "clause_id": oss.CfnIndex.PropertyMappingProperty(type="keyword", index=True),
                    "version": oss.CfnIndex.PropertyMappingProperty(type="integer", index=True),
                }
            ),
        )
        self.index.add_resource_dependency(self.access)

        # ---- Bedrock Knowledge Base over approved clauses (one object per clause, tenant/version/clause_id metadata)
        self.knowledge_base = bedrock.CfnKnowledgeBase(
            self,
            "KnowledgeBase",
            name=f"lira-{env_name}-guidelines",
            role_arn=self.kb_role.role_arn,
            description="Approved outside-counsel guideline clauses; every query filters on tenant",
            knowledge_base_configuration=bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                type="VECTOR",
                vector_knowledge_base_configuration=bedrock.CfnKnowledgeBase.VectorKnowledgeBaseConfigurationProperty(
                    embedding_model_arn=f"arn:aws:bedrock:{self.region}::foundation-model/{EMBEDDING_MODEL}"
                ),
            ),
            storage_configuration=bedrock.CfnKnowledgeBase.StorageConfigurationProperty(
                type="OPENSEARCH_SERVERLESS",
                opensearch_serverless_configuration=bedrock.CfnKnowledgeBase.OpenSearchServerlessConfigurationProperty(
                    collection_arn=self.collection.attr_arn,
                    vector_index_name=INDEX,
                    field_mapping=bedrock.CfnKnowledgeBase.OpenSearchServerlessFieldMappingProperty(
                        vector_field="embedding", text_field="text", metadata_field="metadata"
                    ),
                ),
            ),
        )
        self.knowledge_base.add_resource_dependency(self.index)
        bedrock.CfnDataSource(
            self,
            "Clauses",
            knowledge_base_id=self.knowledge_base.attr_knowledge_base_id,
            name="approved-clauses",
            data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
                type="S3",
                s3_configuration=bedrock.CfnDataSource.S3DataSourceConfigurationProperty(
                    bucket_arn=knowledge.bucket_arn, inclusion_prefixes=["approved/"]
                ),
            ),
            # clauses are already chunked one per object by lira.retrieval.chunk: no further splitting
            vector_ingestion_configuration=bedrock.CfnDataSource.VectorIngestionConfigurationProperty(
                chunking_configuration=bedrock.CfnDataSource.ChunkingConfigurationProperty(chunking_strategy="NONE")
            ),
            server_side_encryption_configuration=bedrock.CfnDataSource.ServerSideEncryptionConfigurationProperty(kms_key_arn=key.key_arn),
        )

        # ---- Guardrail on firm-written text and on answers
        self.guardrail = bedrock.CfnGuardrail(
            self,
            "Guardrail",
            name=f"lira-{env_name}",
            kms_key_arn=key.key_arn,
            description="Prompt attacks in time entries and appeals; PII; grounding of guideline answers",
            blocked_input_messaging="This text contains instructions to the review system and was not processed.",
            blocked_outputs_messaging="The answer could not be grounded in the guidelines.",
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=[
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(type="PROMPT_ATTACK", input_strength="HIGH", output_strength="NONE"),
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(type="INSULTS", input_strength="LOW", output_strength="MEDIUM"),
                ]
            ),
            sensitive_information_policy_config=bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
                pii_entities_config=[
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(type=t, action="ANONYMIZE")
                    for t in ("EMAIL", "PHONE", "US_BANK_ACCOUNT_NUMBER", "INTERNATIONAL_BANK_ACCOUNT_NUMBER", "CREDIT_DEBIT_CARD_NUMBER")
                ]
            ),
            contextual_grounding_policy_config=bedrock.CfnGuardrail.ContextualGroundingPolicyConfigProperty(
                filters_config=[
                    bedrock.CfnGuardrail.ContextualGroundingFilterConfigProperty(type="GROUNDING", threshold=0.8),
                    bedrock.CfnGuardrail.ContextualGroundingFilterConfigProperty(type="RELEVANCE", threshold=0.5),
                ]
            ),
        )
        self.guardrail_version = bedrock.CfnGuardrailVersion(
            self,
            "GuardrailVersion",
            guardrail_identifier=self.guardrail.attr_guardrail_id,
            description="pinned by the release that deployed it",
        )

        # ---- SageMaker: classical NLP models are registered and approved per release
        self.model_group = sagemaker.CfnModelPackageGroup(
            self,
            "NlpModels",
            model_package_group_name=f"lira-{env_name}-nlp",
            model_package_group_description="Block billing, vague, clerical, task-code and appeal-intent classifiers",
        )
        self.training_role = iam.Role(
            self,
            "TrainingRole",
            assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"),
            description="SageMaker training jobs for the classical NLP models",
        )
        artifacts.grant_read_write(self.training_role, "nlp/*")
        key.grant_encrypt_decrypt(self.training_role)

        NagSuppressions.add_resource_suppressions(
            self.kb_role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": "Read access to every clause object under the knowledge bucket is the purpose of this role.",
                }
            ],
            apply_to_children=True,
        )
        NagSuppressions.add_resource_suppressions(
            self.training_role,
            [{"id": "AwsSolutions-IAM5", "reason": "Scoped to the nlp/ prefix of the artifacts bucket and the data key."}],
            apply_to_children=True,
        )

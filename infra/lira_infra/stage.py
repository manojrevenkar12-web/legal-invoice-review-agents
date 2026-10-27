"""All stacks for one environment, in the EU (Frankfurt) by default: the data and the model calls stay in the EU."""

from __future__ import annotations

import aws_cdk as cdk

from lira_infra.ai import AiStack
from lira_infra.foundation import FoundationStack
from lira_infra.service import ServiceStack


def build(
    app: cdk.App, env_name: str, image_tag: str, github_repo: str, alarm_email: str | None = None, env: cdk.Environment | None = None
) -> dict[str, cdk.Stack]:
    env = env or cdk.Environment(
        account=app.node.try_get_context("account") or "111111111111", region=app.node.try_get_context("region") or "eu-central-1"
    )
    base = FoundationStack(app, f"Lira-{env_name}-Foundation", env_name=env_name, env=env)
    ai = AiStack(app, f"Lira-{env_name}-Ai", env_name=env_name, key=base.key, knowledge=base.knowledge, artifacts=base.artifacts, env=env)
    svc = ServiceStack(
        app,
        f"Lira-{env_name}-Service",
        env_name=env_name,
        base=base,
        ai=ai,
        image_tag=image_tag,
        github_repo=github_repo,
        alarm_email=alarm_email,
        env=env,
    )
    base.suppress_cdk_helpers()
    for st in (base, ai, svc):
        cdk.Tags.of(st).add("app", "legal-invoice-review")
        cdk.Tags.of(st).add("env", env_name)
        cdk.Tags.of(st).add("data-classification", "confidential")
    return {"foundation": base, "ai": ai, "service": svc}

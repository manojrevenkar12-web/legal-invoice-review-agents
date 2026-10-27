#!/usr/bin/env python3
"""CDK app: `cdk synth -c env=prod -c imageTag=<git sha> -c githubRepo=owner/repo` (CI does this on main)."""

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks
from lira_infra.stage import build

app = cdk.App()
build(
    app,
    env_name=app.node.try_get_context("env") or "dev",
    image_tag=app.node.try_get_context("imageTag") or "latest",
    github_repo=app.node.try_get_context("githubRepo") or "manojrevenkar12-web/legal-invoice-review-agents",
    alarm_email=app.node.try_get_context("alarmEmail"),
)
cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))
app.synth()

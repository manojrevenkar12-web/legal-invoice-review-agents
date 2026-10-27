"""Invoice intake on AWS.

Law firms (or the e-billing platform) drop LEDES files into s3://<inbox>/<tenant>/<firm>/<name>.ledes.

* ``handler`` (Lambda, S3 ObjectCreated): checks the file at once. A malformed file is moved to rejected/ with the
  reasons next to it, so the firm hears within seconds; a good one is queued on SQS for review.
* ``SqsWorker`` (ECS): long-polls the queue, stores the invoice and runs its review. A message is deleted only after
  the review is stored; failures return to the queue after the visibility timeout and land in the dead-letter
  queue after the configured number of receives.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
from collections.abc import Callable
from typing import Any

from lira import ledes

log = logging.getLogger("lira.ingest")


def _clients() -> tuple[Any, Any]:
    import boto3

    return boto3.client("s3"), boto3.client("sqs")


def handler(event: dict[str, Any], context: Any = None, s3: Any = None, sqs: Any = None) -> dict[str, Any]:
    if s3 is None or sqs is None:
        s3, sqs = _clients()
    queue_url = os.environ["LIRA_REVIEW_QUEUE_URL"]
    results: list[dict[str, Any]] = []
    for rec in event.get("Records", []):
        bucket = rec["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(rec["s3"]["object"]["key"])
        parts = key.split("/")
        if len(parts) < 3 or not key.endswith(".ledes"):
            results.append({"key": key, "status": "ignored"})
            continue
        tenant, firm = parts[0], parts[1]
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8", errors="replace")
        try:
            inv = ledes.parse(body)
            problems = [i.message for i in ledes.check(inv) if i.fatal and i.line_no is None]
            if inv.law_firm_id != firm:
                problems.append(f"LAW_FIRM_ID {inv.law_firm_id} does not match the upload folder {firm}")
        except ledes.LedesError as e:
            inv, problems = None, [i.message for i in e.issues]
        if problems:
            dest = f"rejected/{key}"
            s3.copy_object(Bucket=bucket, Key=dest, CopySource={"Bucket": bucket, "Key": key})
            s3.put_object(
                Bucket=bucket, Key=dest + ".problems.json", Body=json.dumps({"problems": problems}).encode(), ContentType="application/json"
            )
            s3.delete_object(Bucket=bucket, Key=key)
            results.append({"key": key, "status": "rejected", "problems": problems})
            continue
        assert inv is not None
        sqs.send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps({"tenant": tenant, "firm": firm, "bucket": bucket, "key": key, "invoice": inv.number}),
            MessageAttributes={"tenant": {"DataType": "String", "StringValue": tenant}},
        )
        results.append({"key": key, "status": "queued", "invoice": inv.number})
    log.info(json.dumps({"ingest": results}))
    return {"results": results}


class SqsWorker:
    def __init__(self, sqs: Any, s3: Any, queue_url: str, receive: Callable[[str, str, str], Any], review_jobs: Callable[[], Any]):
        self.sqs, self.s3, self.queue_url = sqs, s3, queue_url
        self.receive, self.review_jobs = receive, review_jobs

    async def poll_once(self, wait_seconds: int = 20) -> int:
        import asyncio

        resp = await asyncio.to_thread(
            self.sqs.receive_message,
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=5,
            WaitTimeSeconds=wait_seconds,
            MessageAttributeNames=["All"],
        )
        n = 0
        for m in resp.get("Messages", []):
            body = json.loads(m["Body"])
            text = (await asyncio.to_thread(self.s3.get_object, Bucket=body["bucket"], Key=body["key"]))["Body"].read().decode()
            await self.receive(body["tenant"], text, f"s3:{body['firm']}")
            await self.review_jobs()
            await asyncio.to_thread(self.sqs.delete_message, QueueUrl=self.queue_url, ReceiptHandle=m["ReceiptHandle"])
            n += 1
        return n

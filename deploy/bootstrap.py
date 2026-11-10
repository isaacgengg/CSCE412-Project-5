#!/usr/bin/env python3
"""Idempotent, deterministic infrastructure setup from config.yaml.

Creates whatever is missing and leaves existing resources alone:
  hosted zone (must exist) -> ACM cert (us-east-1) -> private S3 bucket -> OAC
  -> CloudFront distribution -> OAC bucket policy -> Route 53 alias records -> content.

Human-run with an admin-capable profile (the agent profile cannot create resources):
  AWS_PROFILE=default python deploy/bootstrap.py

Manual steps that remain: registering the domain at Namecheap and pasting the hosted
zone's nameservers into Namecheap's Custom DNS (printed by this script).
"""
from __future__ import annotations
import json
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.config import load_config  # noqa: E402
from tools.aws_tools import AWS, CLOUDFRONT_ZONE_ID  # noqa: E402

CACHING_OPTIMIZED = "658327ea-f89d-4fab-a63d-7e88639e58f6"   # AWS managed cache policy


def say(msg):
    print(f"[bootstrap] {msg}")


def main():
    cfg = load_config()
    r53 = boto3.client("route53")
    acm = boto3.client("acm", region_name="us-east-1")
    cf = boto3.client("cloudfront")
    s3 = boto3.client("s3", region_name=cfg.region)

    # 1. Hosted zone
    zones = r53.list_hosted_zones_by_name(DNSName=cfg.domain)["HostedZones"]
    zone = next((z for z in zones if z["Name"].rstrip(".") == cfg.domain), None)
    if not zone:
        zone = r53.create_hosted_zone(Name=cfg.domain, CallerReference=str(time.time()))["HostedZone"]
        say(f"created hosted zone {zone['Id']}")
    zone_id = zone["Id"].split("/")[-1]
    ns = r53.get_hosted_zone(Id=zone_id)["DelegationSet"]["NameServers"]
    say(f"hosted zone {zone_id}; set these as Custom DNS at Namecheap: {', '.join(ns)}")

    # 2. Certificate
    certs = acm.list_certificates(CertificateStatuses=["ISSUED", "PENDING_VALIDATION"])["CertificateSummaryList"]
    cert = next((c for c in certs if c["DomainName"] == cfg.domain
                 and set(cfg.all_names) <= set(c.get("SubjectAlternativeNameSummaries", [cfg.domain]))), None)
    cert_arn = cert["CertificateArn"] if cert else acm.request_certificate(
        DomainName=cfg.domain, SubjectAlternativeNames=cfg.all_names, ValidationMethod="DNS")["CertificateArn"]
    say(f"certificate {cert_arn}")
    for _ in range(12):   # validation records appear a few seconds after the request
        opts = acm.describe_certificate(CertificateArn=cert_arn)["Certificate"]["DomainValidationOptions"]
        if all("ResourceRecord" in o for o in opts):
            break
        time.sleep(5)
    changes = {o["ResourceRecord"]["Name"]: o["ResourceRecord"] for o in opts}
    r53.change_resource_record_sets(HostedZoneId=zone_id, ChangeBatch={"Changes": [
        {"Action": "UPSERT", "ResourceRecordSet": {"Name": rr["Name"], "Type": rr["Type"], "TTL": 300,
                                                   "ResourceRecords": [{"Value": rr["Value"]}]}}
        for rr in changes.values()]})
    say("waiting for certificate validation (needs working NS delegation)...")
    acm.get_waiter("certificate_validated").wait(CertificateArn=cert_arn,
                                                 WaiterConfig={"Delay": 15, "MaxAttempts": 80})
    say("certificate ISSUED")

    # 3. Private bucket
    try:
        s3.head_bucket(Bucket=cfg.bucket)
        say(f"bucket {cfg.bucket} exists")
    except ClientError:
        kw = {} if cfg.region == "us-east-1" else {
            "CreateBucketConfiguration": {"LocationConstraint": cfg.region}}
        s3.create_bucket(Bucket=cfg.bucket, **kw)
        say(f"created bucket {cfg.bucket}")
    s3.put_public_access_block(Bucket=cfg.bucket, PublicAccessBlockConfiguration=dict(
        BlockPublicAcls=True, IgnorePublicAcls=True, BlockPublicPolicy=True, RestrictPublicBuckets=True))
    s3.put_bucket_ownership_controls(Bucket=cfg.bucket, OwnershipControls={
        "Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]})
    region = s3.get_bucket_location(Bucket=cfg.bucket)["LocationConstraint"] or "us-east-1"

    # 4. Origin access control
    oacs = cf.list_origin_access_controls()["OriginAccessControlList"].get("Items", [])
    oac = next((o for o in oacs if o["Name"] == cfg.oac_name), None)
    oac_id = oac["Id"] if oac else cf.create_origin_access_control(OriginAccessControlConfig={
        "Name": cfg.oac_name, "Description": "CSCE 412 site", "SigningProtocol": "sigv4",
        "SigningBehavior": "always", "OriginAccessControlOriginType": "s3"})["OriginAccessControl"]["Id"]
    say(f"OAC {oac_id}")

    # 5. Distribution
    aws = AWS(cfg)
    try:
        dist_id = aws.distribution_id()
        say(f"distribution {dist_id} exists")
    except LookupError:
        origin_id = f"s3-{cfg.bucket}"
        err = [{"ErrorCode": c, "ResponsePagePath": "/404.html", "ResponseCode": "404",
                "ErrorCachingMinTTL": 10} for c in (403, 404)]
        dist = cf.create_distribution(DistributionConfig={
            "CallerReference": f"csce412-{int(time.time())}",
            "Comment": "CSCE 412 Project 5",
            "Enabled": True,
            "Aliases": {"Quantity": len(cfg.all_names), "Items": cfg.all_names},
            "DefaultRootObject": "index.html",
            "Origins": {"Quantity": 1, "Items": [{
                "Id": origin_id, "DomainName": f"{cfg.bucket}.s3.{region}.amazonaws.com",
                "S3OriginConfig": {"OriginAccessIdentity": ""}, "OriginAccessControlId": oac_id}]},
            "DefaultCacheBehavior": {
                "TargetOriginId": origin_id, "ViewerProtocolPolicy": "redirect-to-https",
                "CachePolicyId": CACHING_OPTIMIZED, "Compress": True,
                "AllowedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"],
                                   "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}}},
            "CustomErrorResponses": {"Quantity": 2, "Items": err},
            "ViewerCertificate": {"ACMCertificateArn": cert_arn, "SSLSupportMethod": "sni-only",
                                  "MinimumProtocolVersion": "TLSv1.2_2021"},
            "PriceClass": "PriceClass_100",
            "HttpVersion": "http2",
        })["Distribution"]
        dist_id = dist["Id"]
        aws.distribution_id.cache_clear()
        say(f"created distribution {dist_id} ({dist['DomainName']})")

    # 6-8. Bucket policy, DNS, content
    aws.cfg.distribution_id = dist_id
    say(f"bucket policy: {json.dumps(aws.restore_oac_bucket_policy()['policy'])[:120]}...")
    say(f"alias records: {aws.upsert_alias_records()['records']}")
    sync = aws.s3_sync_site()
    say(f"uploaded {len(sync['uploaded'])} objects, deleted {len(sync['deleted'])}")
    say(f"invalidation: {aws.cloudfront_invalidate(['/*'])['invalidation_id']}")
    say("done. Wait for the distribution to reach 'Deployed', then run: python orchestrator.py verify")


if __name__ == "__main__":
    main()

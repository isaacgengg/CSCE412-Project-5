"""AWS operations used by the agents (S3, CloudFront, Route 53, ACM only).

Read functions are safe for any agent. Write functions are registered as
`consequential` tools, so an agent can only run them after human approval
(except invalidation, which only clears caches).
"""
from __future__ import annotations
import json
import mimetypes
import time
from functools import lru_cache
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from core.config import Config

CLOUDFRONT_ZONE_ID = "Z2FDTNDATAQYW2"   # fixed hosted zone ID for all CloudFront aliases
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("application/javascript", ".js")


class AWS:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.cf = boto3.client("cloudfront")
        self.r53 = boto3.client("route53")
        self.acm = boto3.client("acm", region_name="us-east-1")   # CloudFront certs live here
        self._s3 = None

    # ---------- discovery ----------
    @property
    def s3(self):
        if self._s3 is None:
            self._s3 = boto3.client("s3", region_name=self.bucket_region())
        return self._s3

    @lru_cache
    def bucket_region(self) -> str:
        loc = boto3.client("s3").get_bucket_location(Bucket=self.cfg.bucket)["LocationConstraint"]
        return loc or "us-east-1"

    @lru_cache
    def distribution_id(self) -> str:
        if self.cfg.distribution_id:
            return self.cfg.distribution_id
        for page in self.cf.get_paginator("list_distributions").paginate():
            for d in page.get("DistributionList", {}).get("Items", []):
                if self.cfg.domain in d.get("Aliases", {}).get("Items", []):
                    return d["Id"]
        raise LookupError(f"No CloudFront distribution has alias {self.cfg.domain}")

    @lru_cache
    def hosted_zone_id(self) -> str:
        if self.cfg.hosted_zone_id:
            return self.cfg.hosted_zone_id
        zones = self.r53.list_hosted_zones_by_name(DNSName=self.cfg.domain)["HostedZones"]
        for z in zones:
            if z["Name"].rstrip(".") == self.cfg.domain and not z["Config"]["PrivateZone"]:
                return z["Id"].split("/")[-1]
        raise LookupError(f"No public hosted zone for {self.cfg.domain}")

    def s3_direct_url(self, key: str = "index.html") -> str:
        return f"https://{self.cfg.bucket}.s3.{self.bucket_region()}.amazonaws.com/{key}"

    # ---------- reads ----------
    def s3_public_access_block(self) -> dict:
        try:
            cfg = self.s3.get_public_access_block(Bucket=self.cfg.bucket)["PublicAccessBlockConfiguration"]
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchPublicAccessBlockConfiguration":
                return {"configured": False}
            raise
        return {"configured": True, **cfg, "all_blocked": all(cfg.values())}

    def s3_bucket_policy(self) -> dict:
        try:
            pol = json.loads(self.s3.get_bucket_policy(Bucket=self.cfg.bucket)["Policy"])
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchBucketPolicy":
                return {"exists": False}
            raise
        status = self.s3.get_bucket_policy_status(Bucket=self.cfg.bucket)["PolicyStatus"]
        return {"exists": True, "is_public": status["IsPublic"], "policy": pol}

    def s3_website_hosting(self) -> dict:
        try:
            self.s3.get_bucket_website(Bucket=self.cfg.bucket)
            return {"static_website_hosting_enabled": True}
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchWebsiteConfiguration":
                return {"static_website_hosting_enabled": False}
            raise

    def s3_list_objects(self) -> dict:
        objs = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.cfg.bucket):
            objs += [{"key": o["Key"], "size": o["Size"]} for o in page.get("Contents", [])]
        return {"bucket": self.cfg.bucket, "count": len(objs), "objects": objs}

    def cloudfront_summary(self) -> dict:
        d = self.cf.get_distribution(Id=self.distribution_id())["Distribution"]
        c = d["DistributionConfig"]
        origins = [{
            "id": o["Id"], "domain": o["DomainName"],
            "origin_access_control_id": o.get("OriginAccessControlId") or None,
            "legacy_oai": (o.get("S3OriginConfig") or {}).get("OriginAccessIdentity") or None,
            "is_s3_website_endpoint": ".s3-website" in o["DomainName"],
        } for o in c["Origins"]["Items"]]
        vc = c["ViewerCertificate"]
        return {
            "id": d["Id"], "arn": d["ARN"], "status": d["Status"], "domain_name": d["DomainName"],
            "enabled": c["Enabled"],
            "aliases": c.get("Aliases", {}).get("Items", []),
            "default_root_object": c.get("DefaultRootObject", ""),
            "viewer_protocol_policy": c["DefaultCacheBehavior"]["ViewerProtocolPolicy"],
            "origins": origins,
            "certificate_arn": vc.get("ACMCertificateArn"),
            "minimum_protocol_version": vc.get("MinimumProtocolVersion"),
            "ssl_support_method": vc.get("SSLSupportMethod"),
            "custom_error_responses": [
                {"code": e["ErrorCode"], "page": e.get("ResponsePagePath"), "response_code": e.get("ResponseCode")}
                for e in c.get("CustomErrorResponses", {}).get("Items", [])],
        }

    def acm_certificate(self) -> dict:
        arn = self.cloudfront_summary()["certificate_arn"]
        if not arn:
            return {"error": "distribution has no ACM certificate"}
        c = self.acm.describe_certificate(CertificateArn=arn)["Certificate"]
        return {"arn": arn, "status": c["Status"], "domain": c["DomainName"],
                "subject_alternative_names": c.get("SubjectAlternativeNames", []),
                "not_after": c.get("NotAfter"), "in_use_by": c.get("InUseBy", []),
                "validation": [{"domain": v["DomainName"], "status": v.get("ValidationStatus")}
                               for v in c.get("DomainValidationOptions", [])]}

    def route53_records(self) -> dict:
        recs = self.r53.list_resource_record_sets(HostedZoneId=self.hosted_zone_id())["ResourceRecordSets"]
        out = []
        for r in recs:
            item = {"name": r["Name"].rstrip("."), "type": r["Type"]}
            if "AliasTarget" in r:
                item["alias_target"] = r["AliasTarget"]["DNSName"].rstrip(".")
            else:
                item["values"] = [v["Value"] for v in r.get("ResourceRecords", [])]
            out.append(item)
        return {"hosted_zone_id": self.hosted_zone_id(), "records": out}

    def cloudfront_wait_deployed(self, timeout_s: int = 900) -> dict:
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            st = self.cf.get_distribution(Id=self.distribution_id())["Distribution"]["Status"]
            if st == "Deployed":
                return {"status": st, "waited_s": int(time.time() - t0)}
            time.sleep(15)
        return {"status": "InProgress", "waited_s": int(time.time() - t0), "timed_out": True}

    # ---------- writes ----------
    def _update_distribution(self, mutate) -> dict:
        r = self.cf.get_distribution_config(Id=self.distribution_id())
        cfg = r["DistributionConfig"]
        before = mutate(cfg)
        self.cf.update_distribution(Id=self.distribution_id(), IfMatch=r["ETag"], DistributionConfig=cfg)
        return {"updated": True, "before": before, "note": "CloudFront redeploys in ~2-5 minutes"}

    def set_viewer_protocol_policy(self, policy: str) -> dict:
        assert policy in ("redirect-to-https", "https-only", "allow-all")

        def m(cfg):
            before = cfg["DefaultCacheBehavior"]["ViewerProtocolPolicy"]
            cfg["DefaultCacheBehavior"]["ViewerProtocolPolicy"] = policy
            return before
        return {**self._update_distribution(m), "after": policy}

    def set_default_root_object(self, root_object: str) -> dict:
        def m(cfg):
            before = cfg.get("DefaultRootObject", "")
            cfg["DefaultRootObject"] = root_object
            return before
        return {**self._update_distribution(m), "after": root_object}

    def oac_bucket_policy(self) -> dict:
        arn = self.cloudfront_summary()["arn"]
        return {"Version": "2012-10-17", "Statement": [{
            "Sid": "AllowCloudFrontServicePrincipalReadOnly",
            "Effect": "Allow",
            "Principal": {"Service": "cloudfront.amazonaws.com"},
            "Action": "s3:GetObject",
            "Resource": f"arn:aws:s3:::{self.cfg.bucket}/*",
            "Condition": {"StringEquals": {"AWS:SourceArn": arn}},
        }]}

    def restore_oac_bucket_policy(self) -> dict:
        pol = self.oac_bucket_policy()
        self.s3.put_bucket_policy(Bucket=self.cfg.bucket, Policy=json.dumps(pol))
        return {"applied": True, "policy": pol}

    def enforce_public_access_block(self) -> dict:
        block = dict(BlockPublicAcls=True, IgnorePublicAcls=True, BlockPublicPolicy=True,
                     RestrictPublicBuckets=True)
        self.s3.put_public_access_block(Bucket=self.cfg.bucket, PublicAccessBlockConfiguration=block)
        return {"applied": True, **block}

    def upsert_alias_records(self) -> dict:
        target = self.cloudfront_summary()["domain_name"]
        changes = [{"Action": "UPSERT", "ResourceRecordSet": {
            "Name": name, "Type": rtype,
            "AliasTarget": {"HostedZoneId": CLOUDFRONT_ZONE_ID, "DNSName": target, "EvaluateTargetHealth": False}}}
            for name in self.cfg.all_names for rtype in ("A", "AAAA")]
        r = self.r53.change_resource_record_sets(HostedZoneId=self.hosted_zone_id(),
                                                 ChangeBatch={"Changes": changes})
        self.r53.get_waiter("resource_record_sets_changed").wait(
            Id=r["ChangeInfo"]["Id"], WaiterConfig={"Delay": 10, "MaxAttempts": 30})
        return {"change_id": r["ChangeInfo"]["Id"], "status": "INSYNC",
                "records": [f"{c['ResourceRecordSet']['Name']} {c['ResourceRecordSet']['Type']} -> {target}"
                            for c in changes],
                "note": "Resolvers that cached NXDOMAIN may take up to 15 minutes to see new records."}

    def s3_sync_site(self, delete_extraneous: bool = True) -> dict:
        site: Path = self.cfg.site_dir
        local = {str(f.relative_to(site)).replace("\\", "/"): f for f in site.rglob("*") if f.is_file()}
        uploaded = []
        for key, f in sorted(local.items()):
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            cache = "no-cache" if key.endswith(".html") else "public, max-age=86400"
            self.s3.upload_file(str(f), self.cfg.bucket, key,
                                ExtraArgs={"ContentType": ctype, "CacheControl": cache})
            uploaded.append({"key": key, "content_type": ctype})
        deleted = []
        if delete_extraneous:
            remote = {o["key"] for o in self.s3_list_objects()["objects"]}
            for key in sorted(remote - set(local)):
                self.s3.delete_object(Bucket=self.cfg.bucket, Key=key)
                deleted.append(key)
        return {"bucket": self.cfg.bucket, "uploaded": uploaded, "deleted": deleted}

    def cloudfront_invalidate(self, paths: list[str] | None = None) -> dict:
        paths = paths or ["/*"]
        r = self.cf.create_invalidation(DistributionId=self.distribution_id(), InvalidationBatch={
            "Paths": {"Quantity": len(paths), "Items": paths},
            "CallerReference": f"agent-{int(time.time())}"})
        return {"invalidation_id": r["Invalidation"]["Id"], "status": r["Invalidation"]["Status"], "paths": paths}

"""AWS CostGuard scheduled/on-demand scanner for AWS Lambda.

Configure RESULTS_BUCKET to a dedicated bucket used only for scan reports.
The Lambda execution role should grant the read/write actions documented in
lambda_iam_policy.json (and basic CloudWatch Logs permissions).
"""

import json
import os
from datetime import datetime, timedelta, timezone

import boto3


AWS_REGION = os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "ap-south-1"))
RESULTS_BUCKET = os.environ["RESULTS_BUCKET"]
SNS_TOPIC_ARN = os.getenv("SNS_TOPIC_ARN", "")
EBS_MONTHLY_INR_PER_GB = float(os.getenv("COSTGUARD_EBS_INR_PER_GB_MONTH", "7"))
S3_MONTHLY_INR_SAVING_PER_GB = float(
    os.getenv("COSTGUARD_S3_INR_SAVING_PER_GB_MONTH", "1")
)

ec2 = boto3.client("ec2", region_name=AWS_REGION)
cloudwatch = boto3.client("cloudwatch", region_name=AWS_REGION)
s3 = boto3.client("s3", region_name=AWS_REGION)
sns = boto3.client("sns", region_name=AWS_REGION)


def scan_ec2():
    resources = []
    paginator = ec2.get_paginator("describe_instances")

    for page in paginator.paginate():
        for reservation in page.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                instance_id = instance["InstanceId"]
                state = instance["State"]["Name"]
                name = next(
                    (tag["Value"] for tag in instance.get("Tags", [])
                     if tag["Key"] == "Name"),
                    instance_id,
                )
                avg_cpu = None
                issue = "Normal"
                recommendation = "No action required"

                if state == "running":
                    end_time = datetime.now(timezone.utc)
                    try:
                        metrics = cloudwatch.get_metric_statistics(
                            Namespace="AWS/EC2",
                            MetricName="CPUUtilization",
                            Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
                            StartTime=end_time - timedelta(hours=6),
                            EndTime=end_time,
                            Period=3600,
                            Statistics=["Average"],
                        )
                        points = metrics.get("Datapoints", [])
                    except Exception:
                        points = []

                    if points:
                        avg_cpu = round(
                            sum(point["Average"] for point in points) / len(points), 1
                        )
                        if avg_cpu < 10:
                            issue = "Underutilized"
                            recommendation = (
                                "Review CPU and memory history; consider a smaller instance."
                            )
                    else:
                        issue = "Monitoring"
                        recommendation = "CloudWatch CPU data unavailable or not yet published."
                elif state == "stopped":
                    issue = "Stopped"
                    recommendation = "Confirm the instance is still required."

                resources.append({
                    "type": "EC2",
                    "id": instance_id,
                    "name": name,
                    "details": instance["InstanceType"],
                    "state": state,
                    "cpu": avg_cpu,
                    "issue": issue,
                    "recommendation": recommendation,
                    # Instance pricing is not queried, so do not invent savings.
                    "estimated_monthly_savings_inr": 0,
                })
    return resources


def scan_ebs():
    resources = []
    paginator = ec2.get_paginator("describe_volumes")

    for page in paginator.paginate():
        for volume in page.get("Volumes", []):
            attached = bool(volume.get("Attachments"))
            size_gb = volume["Size"]
            savings = 0 if attached else round(size_gb * EBS_MONTHLY_INR_PER_GB, 2)
            resources.append({
                "type": "EBS",
                "id": volume["VolumeId"],
                "name": volume["VolumeId"],
                "details": f"{size_gb} GB {volume['VolumeType']}",
                "state": "Attached" if attached else "Unattached",
                "cpu": None,
                "issue": "Normal" if attached else "Unused",
                "recommendation": (
                    "No action required" if attached
                    else "Review and delete only if no longer required."
                ),
                "estimated_monthly_savings_inr": savings,
            })
    return resources


def scan_s3_buckets():
    resources = []
    buckets = s3.list_buckets().get("Buckets", [])
    now = datetime.now(timezone.utc)

    for bucket in buckets:
        name = bucket["Name"]
        # Keep the report bucket out of the workload scan to avoid scanning its
        # own growing history as customer data.
        if name == RESULTS_BUCKET:
            continue

        object_count = 0
        old_objects = 0
        old_bytes = 0
        try:
            paginator = s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=name):
                objects = page.get("Contents", [])
                object_count += len(objects)
                for obj in objects:
                    if (now - obj["LastModified"]).days >= 90:
                        old_objects += 1
                        old_bytes += obj.get("Size", 0)

            resources.append({
                "type": "S3",
                "id": name,
                "name": name,
                "details": f"{object_count} objects",
                "state": "Active",
                "cpu": None,
                "issue": "Old Objects" if old_objects else "Normal",
                "recommendation": (
                    "Review an S3 Lifecycle transition to a lower-cost class; "
                    "check retrieval and minimum-storage charges first."
                    if old_objects else "No action required"
                ),
                "old_objects": old_objects,
                "old_object_bytes": old_bytes,
                "estimated_monthly_savings_inr": round(
                    old_bytes / (1024 ** 3) * S3_MONTHLY_INR_SAVING_PER_GB, 2
                ),
            })
        except Exception:
            # Keep a permission/region issue visible instead of failing the
            # entire account scan.
            resources.append({
                "type": "S3",
                "id": name,
                "name": name,
                "details": "Could not list bucket objects",
                "state": "Unknown",
                "cpu": None,
                "issue": "Unable to analyze",
                "recommendation": "Check the Lambda role's s3:ListBucket permission and bucket region.",
                "old_objects": None,
                "old_object_bytes": None,
                "estimated_monthly_savings_inr": None,
            })
    return resources


def build_report():
    resources = scan_ec2() + scan_ebs() + scan_s3_buckets()
    waste_issues = {"Underutilized", "Unused", "Old Objects", "Stopped"}
    waste = [resource for resource in resources if resource["issue"] in waste_issues]
    counts = {
        resource_type: sum(r["type"] == resource_type for r in resources)
        for resource_type in ("EC2", "EBS", "S3")
    }
    return {
        "success": True,
        "resources": resources,
        "total": len(resources),
        "waste": len(waste),
        "suggestions": len(waste),
        "resource_counts": counts,
        "monthly_savings_estimate_inr": round(sum(
            r.get("estimated_monthly_savings_inr") or 0 for r in resources
        ), 2),
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "region": AWS_REGION,
        "savings_estimate_note": (
            f"Planning estimate only. Unattached EBS: ₹{EBS_MONTHLY_INR_PER_GB:g}/GB-month; "
            f"old S3 objects: ₹{S3_MONTHLY_INR_SAVING_PER_GB:g}/GB-month potential tier difference. "
            "EC2 right-sizing is not priced. Confirm regional rates and lifecycle costs."
        ),
    }


def lambda_handler(event, context):
    """Scan resources, save a report, notify SNS subscribers, and return it."""
    if not SNS_TOPIC_ARN:
        raise RuntimeError("Set SNS_TOPIC_ARN to the CostGuard SNS topic ARN.")

    report = build_report()
    timestamp = datetime.now(timezone.utc)
    report_key = f"scans/{timestamp:%Y/%m/%d}/{timestamp:%H%M%S}-{context.aws_request_id}.json"
    scan_source = (event or {}).get("source", "manual-or-unspecified")
    report["report_bucket"] = RESULTS_BUCKET
    report["report_key"] = report_key
    report["scan_source"] = scan_source

    s3.put_object(
        Bucket=RESULTS_BUCKET,
        Key=report_key,
        Body=json.dumps(report, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
        ServerSideEncryption="AES256",
    )

    findings = {}
    for resource in report["resources"]:
        issue = resource["issue"]
        if issue in {"Underutilized", "Unused", "Old Objects", "Stopped"}:
            findings[issue] = findings.get(issue, 0) + 1

    source_label = "scheduled daily" if scan_source == "eventbridge-daily" else "dashboard"
    subject = f"AWS CostGuard {source_label} scan: {report['waste']} finding(s)"
    message_lines = [
        "AWS CostGuard scan complete",
        f"Scan trigger: {source_label}",
        f"Time (UTC): {report['scanned_at']}",
        f"Region: {report['region']}",
        f"Resources analyzed: {report['total']}",
        f"Potential findings: {report['waste']}",
        f"Estimated monthly savings: INR {report['monthly_savings_estimate_inr']}",
        "Finding counts: " + (
            ", ".join(f"{issue}: {count}" for issue, count in sorted(findings.items()))
            if findings else "No flagged resources"
        ),
        f"Report: s3://{RESULTS_BUCKET}/{report_key}",
        "Savings are planning estimates. Review the report and verify actual AWS rates before acting.",
    ]
    sns.publish(
        TopicArn=SNS_TOPIC_ARN,
        Subject=subject[:100],
        Message="\n".join(message_lines),
    )
    report["notification"] = "published"
    return report

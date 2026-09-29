import json
import os
from pathlib import Path

import boto3
from flask import Flask, jsonify, render_template


def load_local_env():
    """Load simple KEY=VALUE settings from .env without overriding the shell."""
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


load_local_env()

app = Flask(__name__)

AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")
LAMBDA_FUNCTION_NAME = os.getenv("COSTGUARD_LAMBDA_FUNCTION")
lambda_client = boto3.client("lambda", region_name=AWS_REGION)


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/scan")
def api_scan():
    try:
        if not LAMBDA_FUNCTION_NAME:
            raise RuntimeError(
                "Set COSTGUARD_LAMBDA_FUNCTION to your deployed Lambda name or ARN."
            )

        response = lambda_client.invoke(
            FunctionName=LAMBDA_FUNCTION_NAME,
            InvocationType="RequestResponse",
            Payload=json.dumps({
                "source": "costguard-dashboard",
                "action": "scan",
            }).encode("utf-8"),
        )

        payload = response["Payload"].read()
        if response.get("FunctionError"):
            raise RuntimeError(payload.decode("utf-8", errors="replace"))

        data = json.loads(payload or b"{}")
        if not data.get("success"):
            raise RuntimeError(data.get("error", "Lambda scan failed."))

        return jsonify(data)

    except Exception as error:
        app.logger.exception("CostGuard Lambda scan failed")
        return jsonify({
            "success": False,
            "error": str(error),
        }), 502


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)

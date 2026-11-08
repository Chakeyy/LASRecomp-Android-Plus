#!/usr/bin/env python3
"""tools/fetch_apk.py: CLI utility to trigger CI workflow and download test artifacts."""
import argparse
import io
import json
import os
import sys
import time
import urllib.request
import zipfile

REPO = "Chakeyy/LASRecomp-Android-Plus"
WORKFLOW_FILE = "build-apk.yml"
TARGET_DIR = os.environ.get("LAS_TARGET_DIR", "/storage/emulated/0/Antigravity/APKs/LASRecomp")

class NoAuthRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        new_req.headers.pop("Authorization", None)
        new_req.headers.pop("authorization", None)
        return new_req

def get_token():
    cred_file = os.path.expanduser("~/.git-credentials")
    if os.path.exists(cred_file):
        with open(cred_file) as f:
            for line in f:
                if "github.com" in line and ":" in line and "@" in line:
                    # https://user:token@github.com
                    parts = line.strip().split("@")[0].split(":")
                    if len(parts) >= 3:
                        return parts[2]
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    print("Error: GitHub token not found in ~/.git-credentials or environment", file=sys.stderr)
    sys.exit(1)

def api_request(path, method="GET", data=None, token=None):
    url = f"https://api.github.com/{path.lstrip('/')}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "LASRecomp-APK-Fetcher"
    }
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req) as resp:
        if resp.status == 204:
            return None
        content = resp.read()
        if resp.headers.get("Content-Type", "").startswith("application/json"):
            return json.loads(content.decode("utf-8"))
        return content

def main():
    parser = argparse.ArgumentParser(description="Trigger CI and download APK")
    parser.add_argument("--rev", default="r1", help="Revision (e.g. r1, r2)")
    parser.add_argument("--ref", default="fix-android-freeze", help="Git branch/ref")
    parser.add_argument("--code", default="120", help="versionCode (default: 120)")
    parser.add_argument("--name", default="1.2.0", help="versionName base (default: 1.2.0)")
    parser.add_argument("--download-only", action="store_true", help="Download from latest run without dispatching")
    args = parser.parse_args()

    token = get_token()
    os.makedirs(TARGET_DIR, exist_ok=True)

    existing_run_ids = set()
    try:
        prior_runs = api_request(f"repos/{REPO}/actions/workflows/{WORKFLOW_FILE}/runs", token=token)
        for r in prior_runs.get("workflow_runs", []):
            existing_run_ids.add(r["id"])
    except Exception as e:
        print(f"Warning: could not fetch prior runs: {e}")

    if not args.download_only:
        print(f"Triggering GitHub Actions workflow {WORKFLOW_FILE} on branch {args.ref}...")
        dispatch_data = {
            "ref": args.ref,
            "inputs": {
                "revision": args.rev,
                "version_code": args.code,
                "version_name": args.name
            }
        }
        api_request(f"repos/{REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches", method="POST", data=dispatch_data, token=token)
        print("Workflow triggered successfully. Waiting for run to start...")
        time.sleep(5)

    print("Locating workflow run...")
    run_id = None
    for _ in range(40):
        runs = api_request(f"repos/{REPO}/actions/workflows/{WORKFLOW_FILE}/runs", token=token)
        workflow_runs = runs.get("workflow_runs", [])
        if args.download_only:
            if workflow_runs:
                run = workflow_runs[0]
                run_id = run["id"]
                print(f"Tracking run #{run.get('run_number')} (ID: {run_id}), status: {run.get('status')}")
                break
        else:
            new_runs = [r for r in workflow_runs if r["id"] not in existing_run_ids]
            if new_runs:
                run = new_runs[0]
                run_id = run["id"]
                print(f"Tracking newly started run #{run.get('run_number')} (ID: {run_id}), status: {run.get('status')}")
                break
        time.sleep(3)

    if not run_id:
        print("Could not find active workflow run.", file=sys.stderr)
        sys.exit(1)

    while True:
        run = api_request(f"repos/{REPO}/actions/runs/{run_id}", token=token)
        status = run.get("status")
        conclusion = run.get("conclusion")
        print(f"Run {run_id} status: {status} ({conclusion or 'in progress'})...")
        if status == "completed":
            if conclusion != "success":
                print(f"Workflow failed with conclusion: {conclusion}", file=sys.stderr)
                sys.exit(1)
            print("Build completed successfully!")
            break
        time.sleep(15)

    # Fetch artifacts
    print("Fetching build artifacts...")
    artifacts = api_request(f"repos/{REPO}/actions/runs/{run_id}/artifacts", token=token)
    artifact_list = artifacts.get("artifacts", [])
    if not artifact_list:
        print("No artifacts found for this run.", file=sys.stderr)
        sys.exit(1)

    target_artifact = artifact_list[0]
    artifact_id = target_artifact["id"]
    artifact_name = target_artifact["name"]
    print(f"Downloading artifact '{artifact_name}' (ID: {artifact_id})...")

    # Download zip
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/actions/artifacts/{artifact_id}/zip",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "LASRecomp-APK-Fetcher"
        }
    )
    opener = urllib.request.build_opener(NoAuthRedirect)
    with opener.open(req) as resp:
        zip_bytes = resp.read()

    # Extract APK
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for fname in z.namelist():
            if fname.endswith(".apk"):
                dest_name = f"LASRecomp-{args.name}-{args.rev}.apk"
                dest_path = os.path.join(TARGET_DIR, dest_name)
                with open(dest_path, "wb") as out_f:
                    out_f.write(z.read(fname))
                print(f"\nSUCCESS: APK saved to {dest_path}")
                print(f"Size: {os.path.getsize(dest_path):,} bytes")
                return

    print("Error: No APK file found inside artifact archive.", file=sys.stderr)
    sys.exit(1)

if __name__ == "__main__":
    main()

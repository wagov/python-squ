#!/usr/bin/env python3
import json
import os
import hashlib
from pkgutil import get_data
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from string import Template
from fire import Fire
from pathlib import Path
from subprocess import check_output, run
from dateutil.parser import isoparse

from fastapi import FastAPI, Response, Body, Request, BackgroundTasks

from sqlitecache import cache, Workspace

secret_api_token = os.environ.get("API_TOKEN")
datalake_blob_prefix = os.environ.get("DATALAKE_BLOB_PREFIX")
os.environ["AZURE_STORAGE_AUTH_MODE"] = "login"

app = FastAPI(title="SIEM Query Utils")


@app.middleware("http")
async def authenticate_request(request: Request, call_next):
    # Middleware to do a simple check of api token vs secure env var
    auth_token = request.query_params.get("auth_token", request.cookies.get("auth_token", "DEBUG"))
    if auth_token not in [secret_api_token]:
        response = Response(content="Invalid auth_token", status_code=403, media_type="text/plain")
    else:
        response = await call_next(request)
        if request.cookies.get("auth_token") != auth_token:
            response.set_cookie("auth_token", auth_token)  # persist auth in a cookie
    return response


@cache()
def azcli(cmd: list):
    "Run a general azure cli cmd"
    cmd = ["az"] + cmd + ["--only-show-errors", "-o", "json"]
    result = check_output(cmd)
    if not result:
        return None
    return json.loads(result)


if os.environ.get("IDENTITY_HEADER"):
    # Use managed service identity to login
    try:
        azcli(["login", "--identity"])
        run(["azcopy", "login", "--identity"])
    except Exception as e:
        # bail as we aren't able to login
        print(e)
        exit()


def loadkql(query):
    "If query starts with https: or kql/ then load it from url or local file and return text"
    if query.startswith("kql/"):
        query = open(query).read().encode("utf-8").strip()
    elif query.startswith("https:"):
        query = check_output(["curl", "-L", query]).decode("utf-8").strip()
    return query


def analytics_query(workspaces: list, query: str, timespan: str = "P7D", outputfilter: str = ""):
    "Queries a list of workspaces using kusto"
    print(f"Log analytics query across {len(workspaces)} workspaces")
    query = loadkql(query)
    chunkSize = 20  # limit to 20 parallel workspaces at a time https://docs.microsoft.com/en-us/azure/azure-monitor/logs/cross-workspace-query#cross-resource-query-limits
    chunks = [
        sorted(workspaces)[x : x + chunkSize] for x in range(0, len(workspaces), chunkSize)
    ]  # awesome list comprehension to break big list into chunks of chunkSize
    # chunks = [[1..10],[11..20]]
    results, cmds = [], []
    for chunk in chunks:
        cmd = ["monitor", "log-analytics", "query", "--workspace", chunk[0], "--analytics-query", query, "--timespan", timespan]
        if len(chunk) > 1:
            cmd += ["--workspaces"] + chunk[1:]
        if outputfilter:
            cmd += ["--query", outputfilter]
        cmds.append(cmd)
    with ThreadPoolExecutor() as executor:
        for result in executor.map(azcli, cmds):
            if result:
                results += result
    return results


@app.get("/listWorkspaces")
@cache(seconds=60 * 60 * 3)  # 3 hr cache
def list_workspaces():
    "Get sentinel workspaces as a list of named tuples"
    workspaces = azcli(["graph", "query", "-q", loadkql("kql/graph-workspaces.kql"), "--first", "1000", "--query", "data[]"])
    # subscriptions is filtered to just those with security solutions installed
    sentinelworkspaces = set()
    # TODO: page on skiptoken if total workspaces exceeds 1000
    # cross check workspaces to make sure they have SecurityIncident tables
    validated = analytics_query(
        [ws["customerId"] for ws in workspaces],
        "kql/distinct-tenantids.kql",
        outputfilter="[].TenantId",
    )
    for ws in workspaces:
        if ws["customerId"] in validated:
            sentinelworkspaces.add(Workspace(**ws))
    return sorted(list(sentinelworkspaces))


@app.get("/simpleQuery")
def simple_query(query: str, name: str, timespan: str = "P7D"):
    "Find first workspace matching name, then run a kusto query against it"
    for workspace in list_workspaces():
        if str(workspace).find(name):
            return analytics_query([workspace.customerId], query, timespan)


def upload_results(results, blobdest, filenamekeys):
    "Uploads a list of json results as individual files split by timegenerated to a blob destination"
    account, dest = blobdest.split("/", 1)
    with tempfile.TemporaryDirectory() as tmpdir:
        dirnames = set()
        for result in results:
            dirname = f"{result['TimeGenerated'].split('T')[0]}"
            dirnames.add(dirname)
            modifiedtime = isoparse(result["TimeGenerated"])
            filename = "_".join([result[key] for key in filenamekeys.split(",")]) + ".json"
            if not os.path.exists(f"{tmpdir}/{dirname}"):
                os.mkdir(f"{tmpdir}/{dirname}")
            with open(f"{tmpdir}/{dirname}/{filename}", "w") as jsonfile:
                json.dump(result, jsonfile, sort_keys=True, indent=2)
            os.utime(
                f"{tmpdir}/{dirname}/{filename}",
                (modifiedtime.timestamp(), modifiedtime.timestamp()),
            )
        cmd = [
            "azcopy",
            "cp",
            tmpdir,
            f"https://{account}.blob.core.windows.net/{dest}",
            "--put-md5",
            "--overwrite=ifSourceNewer",
            "--recursive=true",
            "--as-subdir=false",
        ]
        print(cmd)
        run(cmd)


@app.get("/globalQuery")
def global_query(query: str, tasks: BackgroundTasks, timespan: str = "P7D", count: bool = False, blobdest: str = "", filenamekeys: str = ""):
    """
    Query all workspaces with SecurityIncident tables using kusto.
    If blobdest is provided as a path the first 2 segments are assumed to be the location to save results to <account>/<container>/.../<filename>
    Results are saved as individual .json files, and overwritten if they already exist.
    Filenamekeys are a comma separated list of keys to build filename from
    """
    results = analytics_query([ws.customerId for ws in list_workspaces()], query, timespan)
    if blobdest != "":
        tasks.add_task(upload_results, results, blobdest, filenamekeys)
    if count:
        return len(results)
    else:
        return results


@app.get("/globalStats")
def global_stats(
    query: str,
    timespan: str = "P7D",
    count: bool = False,
    blobdest: str = "",
):
    """
    Query all workspaces with SecurityIncident tables using kusto.
    If blobdest is provided as a path the date will replace the querydate param <account>/<container>/{querydate}/<filename>
    Results are saved as a single json file intended for e.g. powerbi
    """
    results = analytics_query([ws.customerId for ws in list_workspaces()], query, timespan)
    if blobdest != "":
        blobdest = blobdest.format(querydate=datetime.now().date().isoformat())
        account, dest = blobdest.split("/", 1)
        with tempfile.NamedTemporaryFile(mode="w") as uploadjson:
            json.dump(results, uploadjson, sort_keys=True, indent=2)
            uploadjson.flush()
            cmd = ["azcopy", "cp", uploadjson.name, f"https://{account}.blob.core.windows.net/{dest}", "--put-md5", "--overwrite=true"]
            print(cmd)
            run(cmd)
    if count:
        return len(results)
    else:
        return results


email_template = Template(open("templates/email-template.html").read())


def get_datalake_file(path: str):
    url = f"{datalake_blob_prefix}/{path}"
    cmd = ["az", "storage", "blob", "download", "--blob-url", url, "-f", "/dev/stdout", "--max-connections", "1", "--no-progress", "-o", "none"]
    result = check_output(cmd)
    return json.loads(result)


@app.post("/sentinelBeautify")
def sentinel_beautify(data: dict = Body(...)):
    labels = [f"SIEM_Severity:{data['Severity']}", f"SIEM_Status:{data['Status']}", f"SIEM_Title:{data['Title']}"]

    if data.get("Classification"):
        labels.append(f"SIEM_Classification:{data['Classification']}")
    if data.get("ClassificationReason"):
        labels.append(f"SIEM_ClassificationReason:{data['ClassificationReason']}")
    if data.get("ProviderName"):
        labels.append(f"SIEM_ProviderName:{data['ProviderName']}")

    if data.get("Owner"):
        data["Owner"] = json.loads(data["Owner"])
        if data["Owner"].get("email"):
            labels.append(f"SIEM_OwnerEmail:{data['Owner']['email']}")

    if data.get("AdditionalData"):
        data["AdditionalData"] = json.loads(data["AdditionalData"])
        if data["AdditionalData"].get("alertProductNames"):
            labels.append(f"SIEM_alertProductNames:{','.join(data['AdditionalData']['alertProductNames'])}")
        if data["AdditionalData"].get("tactics"):
            labels.append(f"SIEM_tactics:{','.join(data['AdditionalData']['tactics'])}")
        if data["AdditionalData"].get("techniques"):
            labels.append(f"SIEM_techniques:{','.join(data['AdditionalData']['techniques'])}")

    if data.get("AlertIds") and datalake_blob_prefix:
        data["AlertIds"] = json.loads(data["AlertIds"])
        alertdata = []
        for alertid in data["AlertIds"]:
            # below should be able to find all the alerts from the latest day of activity
            try:
                url = f"sentinel_outputs/alerts/{data['LastActivityTime'].split('T')[0]}/{data['TenantId']}_{alertid}.json"
                alert = get_datalake_file(url)
            except Exception as e:  # alert may not exist on day of last activity time
                print(e)
            else:
                try:
                    alert["Entities"] = json.loads(alert.get("Entities", "null"))
                    alert["ExtendedProperties"] = json.loads(alert.get("ExtendedProperties", "null"))
                    alert["RemediationSteps"] = json.loads(alert.get("RemediationSteps", "null"))
                    alertdata.append(alert)
                except Exception as e:
                    print(e)
        data["AlertData"] = alertdata

    urlhash = hashlib.new("sha256")
    urlhash.update(data["IncidentUrl"].encode("utf-8"))
    urlhash = urlhash.hexdigest()
    subject = f"Sentinel Detection - {data['Title']} ({data['Status']}) - urlhash:{urlhash}"
    content = f"<h2>Sentinel Detection - {data['Title']} ({data['Status']})</h2>"
    content += f"<p>Sentinel Incident: <a href='{data['IncidentUrl']}'>{data['IncidentNumber']}</a></p>"

    footer = os.environ.get("FOOTER_HTML", "Set FOOTER_HTML env var to configure this...")
    html = email_template.substitute(title=subject, content=content, footer=footer)

    response = {
        "subject": subject,
        "html": html,
        "labels": labels,
        "urlhash": urlhash,
        "sentinel_data": data,
    }
    return response


def debug_server():
    "Run a debug server on port 8000 that doesn't need auth"
    import uvicorn

    azcli(["extension", "add", "-n", "log-analytics", "-y"])
    azcli(["extension", "add", "-n", "resource-graph", "-y"])
    os.environ["API_TOKEN"] = "DEBUG"
    uvicorn.run("main:app", host="0.0.0.0", port=8000, log_level="debug", reload=True)


if __name__ == "__main__":
    Fire(
        {
            "listWorkspaces": list_workspaces,
            "simpleQuery": simple_query,
            "globalQuery": global_query,
            "globalStats": global_stats,
            "debug": debug_server,
        }
    )
elif not secret_api_token or secret_api_token == "changeme":
    exit("Please set API_TOKEN env var to run web server")

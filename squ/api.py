"""
Main api endpoints to be added to a fastapi app
"""
# pylint: disable=logging-fstring-interpolation, broad-exception-raised, invalid-name
import base64
import hashlib
import hmac
import importlib
import io
import json
import os
import shlex
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from enum import Enum
from json import JSONDecodeError
from pathlib import Path
from random import shuffle
from string import Template
from typing import Optional

import httpx_cache
import pandas
import requests
from abuseipdb_wrapper import AbuseIPDB
from atlassian import Jira
from azure.kusto.data.helpers import dataframe_from_result_table
from dateutil.parser import isoparse
from requests.exceptions import ReadTimeout
from upath import UPath

from .azcli import adx_query, adxtable2df, azcli, cache, clean_path, httpx_client, logger, settings


class OutputFormat(str, Enum):
    """
    Output formats for query results
    """

    JSON = "json"
    CSV = "csv"
    LIST = "list"
    DF = "df"
    CMD = "cmd"


def abuse_client():
    """
    Returns an abuseipdb client
    """
    return AbuseIPDB(API_KEY=os.environ["ABUSEIPDB_API_KEY"])


def jira_client():
    """
    Returns a jira client
    """
    return Jira(
        url=os.environ["JIRA_URL"],
        username=os.environ["JIRA_USERNAME"],
        password=os.environ["JIRA_PASSWORD"],
    )


def clear_cached_adxqueries():
    """
    Clears all cached adx queries
    """
    for table in adx_query(".show tables").raw_rows:
        if table[0].startswith("cached_adxquery__"):
            adx_query(f".drop table {table[0]}")


@cache.memoize()
def cached_adxquery(query, latest_scalar="summarize max(ingestion_time())", max_query_age="1d"):
    """
    Convenience function to run a query and cache in data explorer for a period
    """
    max_query_age = pandas.Timestamp("now", tz="UTC") - pandas.Timedelta(max_query_age)
    cachetable = "cached_adxquery__" + hashlib.sha256(query.encode("utf8")).hexdigest()
    try:
        cache_age = adx_query(f"{cachetable} | {latest_scalar}").raw_rows[0][0]
        assert pandas.Timestamp(cache_age) > max_query_age
    except Exception:  # pylint: disable=broad-except
        adx_query(f".set-or-replace {cachetable} with(folder='cached_adxquery', recreate_schema=true, docstring=```{query}```) <| {query}")
    return adxtable2df(adx_query(cachetable))


def datalake_json(path: str, content=None, modified_key: Optional[str] = None) -> dict:
    """
    Reads or writes a json file to the datalake.

    Args:
        path (str): Path to read or write.
        content (_type_, optional): Content to write. Defaults to None.
        modified_key (Optional[str], optional): Key to use for comparing the modified time.
            Defaults to None.

    Returns:
        dict: The json content.
    """
    # retrieves or uploads a json file from the datalake
    path = settings("datalake_path") / clean_path(path)
    if content is None:
        return json.loads(path.read_text())
    elif path.exists():
        existing_content = json.loads(path.read_text())
        # Contrast the actual blob content for its modified time
        source_mtime, dest_mtime = isoparse(content[modified_key]), isoparse(
            existing_content[modified_key]
        )
        if source_mtime <= dest_mtime:
            # if the source is older than the destination, return the destination without uploading
            return existing_content
    logger.debug(f"Uploading {path}.")
    path.write_text(json.dumps(content, sort_keys=True, indent=2))
    return content


@cache.memoize()
def load_kql(query: str) -> str:
    """
    - If query starts with kql/ then load it from a package resource and return text
    - If query starts with kql:// then load it from {KQL_BASEURL} and return text
    """
    if query.startswith("kql/"):
        path = Path(__package__) / Path(clean_path(query))
        logger.debug(f"loading kql from {path}")
        query = importlib.resources.read_text(
            package=str(path.parent).replace("/", "."), resource=path.name
        ).strip()
    # If query starts with kql:// then load it from KQL_BASEURL
    elif query.startswith("kql://"):
        base_url = os.environ["KQL_BASEURL"]
        path = clean_path(query.replace("kql://", "", 1))
        url = f"{base_url}/{path}"
        logger.debug(f"loading kql from {url}")
        query = requests.get(url, timeout=10).text.strip()
    return query


def analytics_query(
    workspaces: list[str], query: str, timespan: str = "P7D", group_queries=True, dry_run=False
):
    "Queries a list of workspaces using kusto"
    query = load_kql(query)
    cmd_base = [
        "monitor",
        "log-analytics",
        "query",
        "--analytics-query",
        query,
        "--timespan",
        timespan,
    ]
    if group_queries or len(workspaces) == 1 or dry_run:
        cmd = cmd_base + ["--workspace", workspaces[0]]
        if len(workspaces) > 1:
            cmd += ["--workspaces"] + workspaces[1:]
        if dry_run:
            return shlex.join(["az"] + cmd)
        try:
            return azcli(cmd)  # big grouped query
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(f"{exc}: falling back to individual queries")
    # run each query separately and stitch results, 20 at a time.
    results = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            workspace: executor.submit(azcli, cmd_base + ["--workspace", workspace])
            for workspace in workspaces
        }
        for workspace, future in futures.items():
            try:
                result = future.result()
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning(exc)
                result = []
            for item in result:
                item.update({"TenantId": workspace})
            results += result
    return results


@cache.memoize(ttl=60 * 60 * 3)  # 3 hr cache
def list_workspaces(fmt: OutputFormat = OutputFormat.LIST, agency="ALL"):
    "Get sentinel workspaces from {datalake}/notebooks/lists/SentinelWorkspaces.csv"
    # return workspaces dataframe from the datalake
    dataframe = (
        pandas.read_csv(
            (settings("datalake_path") / "notebooks/lists/SentinelWorkspaces.csv").open()
        )
        .join(
            pandas.read_csv(
                (settings("datalake_path") / "notebooks/lists/SecOps Groups.csv").open()
            ).set_index("Alias"),
            on="SecOps Group",
            rsuffix="_secops",
        )
        .rename(columns={"SecOps Group": "alias", "Domains and IPs": "domains"})
    )
    dataframe = dataframe.dropna(subset=["customerId"]).sort_values(by="alias")
    if agency != "ALL":
        dataframe = dataframe[dataframe["alias"] == agency]
    if fmt == OutputFormat.LIST:
        return list(dataframe.customerId)
    elif fmt == OutputFormat.JSON:
        return dataframe.fillna("").to_dict("records")
    elif fmt == OutputFormat.CSV:
        return dataframe.to_csv()
    elif fmt == OutputFormat.DF:
        return dataframe


def workspace_details():
    details_file = settings("datalake_path") / "notebooks/lists/workspace_details.json"
    if (
        details_file.exists()
        and datetime.utcnow().timestamp() - details_file.stat().st_mtime < 60 * 60 * 3
    ):
        return json.loads(details_file.read_text())
    wsdetail = []
    for wsdata in list_workspaces("json"):
        customerId = wsdata["customerId"]
        sub = wsdata["subscription"]
        ws = azcli(
            [
                "monitor",
                "log-analytics",
                "workspace",
                "list",
                "--subscription",
                sub,
                "--query",
                f"[?customerId == '{customerId}']",
            ]
        )[0]
        ws["id"] = ws["id"].lower()
        ws["name"] = ws["name"].lower()
        ws["ingest_function"] = f"{ws['customerId'].replace('-', '_')}_incoming"
        wsdetail.append(ws)
    details_file.write_text(json.dumps(wsdetail))
    return wsdetail


def list_domains(agency: str, fmt="text") -> str:
    """
    Returns a list of domains for a given agency.

    Args:
        agency (str): Agency name.
        fmt (str, optional): Output format. Defaults to "text".

    Returns:
        str: List of domains.
    """
    secops = list_workspaces(OutputFormat.DF)
    secops = secops[secops.alias == agency]  # filter by agency
    workspaces = list(secops.customerId.dropna())
    if not workspaces:
        raise Exception(f"agency {agency} not found")
    existing_domains = set(str(secops.domains.dropna().sum()).strip().split("\n"))
    if existing_domains == set("0"):
        existing_domains = set()
    active_domains = analytics_query(workspaces, "kql/distinct-domains.kql")
    if not active_domains:
        active_domains = set()
    else:
        active_domains = set(pandas.DataFrame.from_records(active_domains).domain.values)
    all_domains = sorted(list(active_domains.union(existing_domains)))
    domains = []
    for domain in all_domains:  # filter out subdomains
        for check in all_domains:
            if domain != check and domain.endswith(check):
                break
        else:
            domains.append(domain.strip())
    if fmt == "text":
        return "\n".join(domains)
    elif fmt == "json":
        return domains


def upload_results(results, blobdest, filenamekeys):
    "Uploads a list of json results as files split by timegenerated to a blob destination"
    if not results:
        return
    blobdest = clean_path(blobdest)
    with ThreadPoolExecutor(max_workers=32) as executor:
        futures = []
        for result in results:
            dirname = f"{result['TimeGenerated'].split('T')[0]}"
            filename = "_".join([result[key] for key in filenamekeys.split(",")]) + ".json"
            futures.append(
                executor.submit(
                    datalake_json,
                    path=f"{blobdest}/{dirname}/{filename}",
                    content=result,
                    modified_key="TimeGenerated",
                )
            )
    logger.debug(f"Uploaded {len(results)} results.")


def atlaskit_client():
    """
    Client for the atlaskit API
    """
    return httpx_cache.Client(base_url="http://127.0.0.1:3000")


class AtlaskitFmt(str, Enum):
    """
    Conversion formats for atlaskit
    """

    MARKDOWN = "md"
    JSON = "adf"
    WIKIMARKUP = "wiki"


def build_la_signature(
    customer_id: str,
    shared_key: str,
    date: str,
    content_length: int,
    method: str,
    content_type: str,
    resource,
) -> str:
    """
    Build the signature string for the Log Analytics Data Collector API.

    Args:
        customer_id (str): The workspace ID.
        shared_key (str): The primary or the secondary Connected Sources client authentication key.
        date (str): The current date in RFC1123 format.
        content_length (int): The length of the request body in bytes.
        method (str): The HTTP method (GET, POST, etc.).
        content_type (str): The content type of the request.
        resource (str): The resource URI.

    Returns:
        str: The signature string.
    """
    x_headers = "x-ms-date:" + date
    string_to_hash = (
        method
        + "\n"
        + str(content_length)
        + "\n"
        + content_type
        + "\n"
        + x_headers
        + "\n"
        + resource
    )
    bytes_to_hash = bytes(string_to_hash, encoding="utf-8")
    decoded_key = base64.b64decode(shared_key)
    encoded_hash = base64.b64encode(
        hmac.new(decoded_key, bytes_to_hash, digestmod=hashlib.sha256).digest()
    ).decode()
    authorization = f"SharedKey {customer_id}:{encoded_hash}"
    return authorization


def query_all(
    query: str,
    group_queries: bool = True,
    timespan: str = "P7D",
    fmt: OutputFormat = OutputFormat.JSON,
):
    """
    Query all workspaces from `/listWorkspaces` using kusto.
    """
    if fmt == OutputFormat.CMD:
        cmd = analytics_query(
            list_workspaces(), query, timespan, group_queries=group_queries, dry_run=True
        )
        return cmd
    results = analytics_query(list_workspaces(), query, timespan, group_queries=group_queries)
    if fmt == OutputFormat.JSON:
        return results
    elif fmt in [OutputFormat.CSV, OutputFormat.LIST]:
        return pandas.DataFrame.from_dict(results).to_csv()
    elif fmt == OutputFormat.DF:
        return pandas.DataFrame.from_dict(results)


def zip_data(obj: dict[pandas.DataFrame]) -> bytes:
    """
    Creates a zipped set of json files from a dict of dataframes.
    If any of the keys don't end in .json they are written as
    plain text instead of as a dataframe to json.

    Args:
        obj (dict[pandas.DataFrame]): Dictionary of dataframes to write.

    Returns:
        bytes: Zipped bytes of the data.
    """
    zip_bytes = io.BytesIO()
    now = datetime.utcnow().timetuple()
    with zipfile.ZipFile(zip_bytes, "a") as zip_file:
        for name, dframe in obj.items():
            if not name.endswith(".json"):
                txt_info = zipfile.ZipInfo(f"{name}", date_time=now)
                zip_file.writestr(txt_info, dframe, zipfile.ZIP_DEFLATED)
                continue
            dframe = dframe.convert_dtypes()  # enhance fields where possible
            for col, dtype in zip(dframe.columns, dframe.dtypes):
                for timestr in ["seen", "updated", "created", "date", "time"]:
                    if timestr in col.lower():  # enhance dates if possible
                        if "str" in str(dtype).lower():
                            try:
                                dframe[col] = pandas.to_datetime(dframe[col])
                            except ValueError:
                                pass
                        elif "int" in str(dtype).lower():
                            try:
                                dframe[col] = pandas.to_datetime(dframe[col], unit="s")
                            except ValueError:
                                pass
                if dtype == "object":  # simplify nested objects
                    dframe[col] = dframe[col].astype("string")
            json_info = zipfile.ZipInfo(f"{name}", date_time=now)
            json_str = dframe.to_json(orient="records", date_format="iso")
            zip_file.writestr(json_info, json_str, zipfile.ZIP_DEFLATED)
    return zip_bytes.getvalue()


def load_dataframes(path: UPath) -> dict[pandas.DataFrame]:
    """
    Reads a zip file containing json files into a dictionary of dataframes.

    Args:
        path (UPath): Path to zip file

    Returns:
        dict[pandas.DataFrame]: Dictionary of dataframes
    """
    logger.debug(f"Decompressing {path}")
    obj = {}
    with zipfile.ZipFile(path, "r") as mem_zipfile:
        for name in mem_zipfile.namelist():
            if name.endswith(".json"):
                with mem_zipfile.open(name) as json_file:
                    obj[name[:-5]] = pandas.read_json(json_file, orient="records")
    return obj


def kql2df(
    kql: str, timespan: str, workspaces: list[str], attempt=0, max_attempts=5
) -> pandas.DataFrame:
    """
    Load data from Sentinel into a dataframe.

    Args:
        kql (str): Kusto query to run.
        timespan (str): Timespan to query.
        workspaces (list[str]): List of workspaces to query.

    Returns:
        pandas.DataFrame: Dataframe of results.
    """
    table = kql.split("\n")[0].split(" ")[0].strip()
    try:
        data = analytics_query(workspaces=workspaces, query=kql, timespan=timespan)
        assert data and (len(data)) > 0
        data = pandas.json_normalize(data, max_level=1)
    except ReadTimeout as exc:
        if attempt < max_attempts:
            logger.warning(f"Timeout: {exc}, retrying...")
            time.sleep(1 + attempt * 2)
            return kql2df(kql, timespan, workspaces, attempt=attempt + 1, max_attempts=max_attempts)
        else:
            raise exc
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning(f"{exc}: No data for {table} in {workspaces}")
        data = pandas.DataFrame.from_records([{f"{table}": f"No Data in timespan {timespan}"}])
    return data


def httpx_api(apiname: str) -> httpx_cache.Client:
    """
    Returns a httpx client for the given api configured using keyvault session.

    Args:
        apiname (str): Name of the api to use.

    Returns:
        httpx_cache.Client: httpx client for the api.
    """
    return httpx_client(settings("keyvault_session")["session"][f"proxy_{apiname}"])


def runzero2df(params: dict) -> pandas.DataFrame:
    """
    Get the runzero services for a given agency.

    Args:
        params (dict): Parameters to pass to the runzero api.

    Returns:
        pandas.DataFrame: Services for an agency as a dataframe.
    """
    logger.debug(f"Querying runzero services: {params}")
    response = httpx_api("runzero-v1.0").get("/export/org/services.jsonl", params=params)
    rows = pandas.read_json(response.text, lines=True).to_dict(  # pylint: disable=no-member
        orient="records"
    )
    if len(rows) == 0:
        dframe = pandas.DataFrame.from_records(
            [{"External Internet Services": f"No Data found for {params}"}]
        )
    else:
        dframe = pandas.json_normalize(rows, max_level=1)
        for col in dframe.columns:  # drop columns about scanning infrastructure
            if "agent_" in col or "site_" in col:
                dframe = dframe.drop(columns=col)
    return dframe


def report_zipjson(query_config: dict, agency: str, timespan: str):
    """
    Run a set of queries and return the results as a collection of json in a zip.

    Args:
        query_config (dict): Dictionary of queries to run.
        agencies (pandas.DataFrame): Dataframe of agencies to query.
        timespan (str): Timespan to query.
    """
    logger.debug(f"Querying sentinel: {agency}")
    wsids = list_workspaces(OutputFormat.LIST, agency)
    agency_info = list_workspaces(OutputFormat.DF, agency)
    futures, text_files = {}, {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        for name, kql in query_config["kql"].items():
            futures[f"{name}.json"] = executor.submit(kql2df, kql, timespan, wsids)
            text_files[f"{name}.kql"] = kql
        report_data = {name: future.result() for name, future in futures.items()}
    report_data.update(text_files)
    if agency == "ALL":
        logger.debug("Querying runzero assets: ALL")
        response = httpx_api("runzero-v1.0").get(
            "/export/org/assets.csv"
        )  # use csv as memory requirement is lower
        runzero_assets = pandas.read_csv(io.StringIO(response.text))
        report_data["Internet Exposed Assets.json"] = runzero_assets
    else:
        domains = list_domains(agency)
        agency_info["domains"] = domains
        runzero_query = " OR ".join([f'vhost:"%{domain}"' for domain in domains.split("\n")])
        report_data["Internet Exposed Services.search.runzero"] = (
            "# Export RunZero Services\n" + runzero_query
        )
        report_data["Internet Exposed Services.json"] = runzero2df({"search": runzero_query})
    report_data["Agency Info.json"] = agency_info
    return zip_data(report_data)


@cache.memoize(ttl=60 * 60)
def data_collector(target_workspace: str = None) -> tuple[str]:
    """
    Retreives credentials for a target workspace.

    Args:
        target_workspace (str, optional): Defaults to settings("datalake_collector_connstring").

    Returns:
        tuple[str] (customer_id, shared_key): workspace id and workspace key.
    """
    if not target_workspace:
        target_workspace = settings("data_collector_connstring")
    else:
        target_workspace = settings("datalake_subscription") + "/" + target_workspace
    subscription, resourcegroup, workspacename = target_workspace.split("/")
    az_workspace = [
        "--subscription",
        subscription,
        "--resource-group",
        resourcegroup,
        "--name",
        workspacename,
    ]
    customer_id = azcli(["monitor", "log-analytics", "workspace", "show"] + az_workspace)[
        "customerId"
    ]
    shared_key = azcli(["monitor", "log-analytics", "workspace", "get-shared-keys"] + az_workspace)[
        "primarySharedKey"
    ]
    return customer_id, shared_key


def get_dx_kql(name):
    """
    Returns the contents of a dxkql file in the dataexplorer folder.
    """
    local_path = Path(__file__).parent.parent / "wasoc-notebook/notebooks/kql/dataexplorer"
    remote_path = settings("datalake_path") / "notebooks/wasoc-notebook/kql/dataexplorer"
    if os.environ.get("LOG_LEVEL").lower() == "debug" and (local_path / name).exists():
        logger.debug(f"Dev mode (LOG_LEVEL: DEBUG) - Using local {name}")
        return (local_path / name).read_text()
    else:
        return (remote_path / name).read_text()


def configure_datalake_hot():
    """
    Configures the hot data lake for ingestion. Should be scheduled to run once per day.

    Note separately, cluster policy should be tweaked for ingestion performance as below:

    .alter-merge cluster policy capacity ```{
        "IngestionCapacity": {
            "ClusterMaximumConcurrentOperations": 512,
            "CoreUtilizationCoefficient": 16
        },
        "StreamingIngestionPostProcessingCapacity": {
            "MaximumConcurrentOperationsPerNode": 40
        }
    }```
    """
    ingest_func = Template(get_dx_kql("ingest_func.kql"))
    clusters = []
    for workspace in workspace_details():
        cluster = f"cluster('https://ade.loganalytics.io{workspace['id']}').database('{workspace['name']}').table(tbl)"
        clusters.append(cluster)
    clusters = ", ".join(clusters)
    global_ingest = Template(get_dx_kql("ingest_func_global.kql")).substitute(clusters=clusters)
    logger.debug(global_ingest)
    return dataframe_from_result_table(
        adx_query([global_ingest] + [ingest_func.substitute(**ws) for ws in workspace_details()])
    )


def ingest_datalake_hot():
    """
    Ingests the hot data lake into the data lake. Should be scheduled to run every 1 minute.
    """
    ingest_tables = Template(get_dx_kql("ingest_tables.kql"))
    with ThreadPoolExecutor(max_workers=6) as executor:
        ingest_queries = get_dx_kql("ingest_tables_global.kql").split("\n\n")
        for ws in workspace_details():
            ingest_queries += ingest_tables.substitute(**ws).strip().split("\n\n")
        shuffle(ingest_queries)
        logger.debug(
            f"Running {len(ingest_queries)} ingest queries at {datetime.now().isoformat()}"
        )
        futures = []
        start = time.time()
        for query in ingest_queries:
            futures.append((query, executor.submit(adx_query, query)))
        seconds = 0
        while True:
            seconds = time.time() - start
            running = [f.running() for q, f in futures].count(True)
            if int(seconds) % 30 == 0:
                done = [f.done() for q, f in futures].count(True)
                logger.debug(
                    f"{running} running, {done} done, {len(futures)} total, time: {seconds:.1f}s"
                )
            if running == 0:
                break
            time.sleep(1)
        results = []
        for query, future in futures:
            if future.exception():
                logger.error(f"{query} failed with {future.exception()}"[:300])
            else:
                results.append(dataframe_from_result_table(future.result()))
        df = pandas.concat(results)
        logger.debug(df.sum(numeric_only=True).to_string())
        hotcache = adx_query("find where isnotempty(source_ingestion_time) | count")[0]["Count"]
        logger.info(f"Ingest done in {seconds:.1f}s. Hot cache: {hotcache} records")
    ingest_stats = get_dx_kql("ingest_stats.kql")
    logger.debug(
        "Ingestion delay > 3 hrs:\n"
        + dataframe_from_result_table(adx_query(ingest_stats)).to_string()
    )


def export_jira_issues():
    client = httpx_api("jira-3")

    def getissues(start_at, jql):
        response = client.get(
            "search", params={"jql": jql, "fields": "*all", "startAt": start_at, "maxResults": 100}
        ).json()
        next_start = response["startAt"] + response["maxResults"]
        total_rows = response["total"]
        if next_start > total_rows:
            next_start = total_rows
        issues = response["issues"]
        return next_start, total_rows, issues

    def save_date_issues(
        after_date: pandas.Timestamp, path=settings("datalake_path") / "jira_outputs" / "issues"
    ):
        fromdate = after_date
        todate = after_date + pandas.to_timedelta("1d")
        jql = (
            f"updated >= {fromdate.date().isoformat()} and updated <"
            f" {todate.date().isoformat()} order by key"
        )
        output = path / f"{fromdate.date().isoformat()}" / "issues.parquet"
        if output.exists() and fromdate < pandas.Timestamp.now() - pandas.to_timedelta("1d"):
            # skip previously dumped days except for last day
            return None
        start_at, total_rows = 0, -1
        dataframes = []
        while start_at != total_rows:
            start_at, total_rows, issues = getissues(start_at, jql)
            dataframes.append(pandas.DataFrame(issues))
            if start_at == 100:
                logger.info(f"{total_rows} to load")
        if total_rows > 1:
            df = pandas.concat(dataframes)
            df["fields"] = df["fields"].apply(json.dumps)
            logger.info(f"saving {output}")
            try:
                df.to_parquet(output.open("wb"))
            except Exception as exc:
                print(exc)
            return df
        else:
            return None

    after = pandas.Timestamp.now() - pandas.to_timedelta("7d")
    until = pandas.Timestamp.now() + pandas.to_timedelta("1d")

    while after < until:
        save_date_issues(after)
        after += pandas.to_timedelta("1d")


def update_jira_issues(start_after="ago(3h)"):
    from .sentinel_beautify import sentinel_beautify_local

    client = httpx_api("jira-3")

    # +
    def jiradata(siemrefs):
        jql = " OR ".join(f'"SIEM Reference[Short text]" ~ "{siemref}"' for siemref in siemrefs)
        response = client.post(
            "search",
            json={
                "jql": jql,
                "fields": [
                    "summary",
                    "status",
                    "customfield_10061",
                    "customfield_10063",
                    "customfield_10064",
                    "customfield_10065",
                    "customfield_10039",
                    "customfield_10010",
                    "requestType",
                    "updated",
                ],
            },
        )
        try:
            return response.json().get("issues")
        except JSONDecodeError:
            logger.warning(response.headers)
            logger.warning(response.text)
            raise

    def checkrow(row):
        if not isinstance(row["jira"], dict):
            return "create"
        fields = row["jira"]["fields"]
        labels = dict(l.split(":") for l in fields["customfield_10065"] if len(l.split(":")) == 2)
        current = (
            row["Title"] == fields["customfield_10063"]
            and row["Severity"] == labels["SIEM_Severity"]
            and row["Status"] == labels["SIEM_Status"]
            and pandas.to_datetime(fields["updated"]) > pandas.to_datetime(row["TimeGenerated"])
        )
        if current:
            return "current"
        else:
            return "update"

    # +
    def incidents(after="ago(1h)", rows=1000):
        df = adxtable2df(
            adx_query(
                f"""SecurityIncident
        | summarize arg_max(TimeGenerated, *) by IncidentNumber, TenantId
        | where TimeGenerated >= {after}
        | order by TimeGenerated asc
        | take {rows}"""
            )
        )
        if df.empty:
            return df
        df["siemref"] = df["TenantId"] + "_" + df["IncidentNumber"].astype(str)
        dfs = [df[i : i + 40] for i in range(0, df.shape[0], 40)]
        for df in dfs:
            issues = {
                issue["fields"]["customfield_10061"]: issue
                for issue in jiradata(list(df["siemref"]))
            }
            df["jira"] = df["siemref"].map(issues)
        df = pandas.concat(dfs)
        df["sync_action"] = df.apply(checkrow, axis=1)
        return df

    def alerts(alertids, tenantid):
        query = f"""SecurityAlert
        | summarize arg_max(TimeGenerated, *) by SystemAlertId, TenantId
        | where SystemAlertId in ('{"', '".join(alertids)}') and TenantId == '{tenantid}'
        | order by TimeGenerated desc
        """
        return adxtable2df(adx_query(query)).to_dict(orient="records")

    # -

    def update_jira(df):
        df = df[df["sync_action"] != "current"].astype({"IncidentNumber": "string"})
        issue_url = str(client.base_url).replace("api/3", "api/2/issue")
        with ThreadPoolExecutor(max_workers=8) as executor:
            df["AlertData"] = list(executor.map(alerts, df["AlertIds"], df["TenantId"]))
        for index, row in df.iterrows():
            sb_data = sentinel_beautify_local(row.to_dict())
            jira_dict = {
                "fields": {
                    "customfield_10002": [int(sb_data["jira_orgid"])],
                    "customfield_10061": row["siemref"],
                    "customfield_10063": sb_data["sentinel_data"]["Title"],
                    "customfield_10064": sb_data["sentinel_data"]["IncidentUrl"],
                    "customfield_10065": sb_data["labels"],
                    "customfield_10071": sb_data["secops_status"],
                    "customfield_10039": None,
                    "description": sb_data["wikimarkup"].decode("utf8"),
                    "issuetype": {"id": 10001},
                    "customfield_10010": "soc/6066f033-446e-4113-a76f-b5e2d77ff296",
                    "project": {"key": "SOC"},
                    "summary": sb_data["subject"][:254],
                }
            }
            if row["sync_action"] == "create":
                response = client.post(issue_url, json=jira_dict)
            elif row["sync_action"] == "update":
                jira_dict["fields"].pop("customfield_10010")  # don't set requestType on update
                response = client.put(issue_url + "/" + row["jira"]["key"], json=jira_dict)
            else:
                logger.warning(row["sync_action"])
            if response.status_code > 299:
                logger.info(response)
            else:
                logger.debug(response.text)
        if not df.empty:
            upload_results(
                df.to_dict(orient="records"),
                "sentinel_outputs/incidents",
                "TenantId,IncidentNumber",
            )
        return df

    # + tags=[]
    after = start_after
    df = incidents(after=after)
    while not df.empty and df.shape[0] > 1:
        logger.info(f"Latest incident seen: {df.TimeGenerated.max()}")
        logger.info(df.groupby("sync_action").size())
        update_jira(df)
        after = f"todatetime('{df.TimeGenerated.max()}')"
        df = incidents(after=after)

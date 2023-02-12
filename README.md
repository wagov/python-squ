# python-squ

Basic usage below

```python
from squ import api
import pandas

# Load workspace info from storage
api.list_workspaces()

# Kusto query to Sentinel workspaces via Azure Lighthouse
api.query_all("kusto query | take 20")

# Kusto query to ADX
api.adxtable2df(api.adx_query("kusto query | take 20"))

# 3rd party http api
response = api.httpx_api("runzero-v1.0").get("/export/org/services.jsonl")
rows = pandas.read_json(response.text, lines=True).to_dict(orient="records")
df = pandas.json_normalize(rows, max_level=1)
```
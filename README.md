# python-squ

## Installation

Install with pip/poetry as below, or add `https://github.com/wagov/python-squ/archive/refs/heads/main.tar.gz` to your `requirements.txt`.

```bash
pip install https://github.com/wagov/python-squ/archive/refs/heads/main.tar.gz
# OR
poetry add https://github.com/wagov/python-squ/archive/refs/heads/main.tar.gz
```

## Usage

```python
from squ import api
import pandas as pd

# Load workspace info from storage
df = api.list_workspaces(fmt="df")

# Kusto query to Sentinel workspaces via Azure Lighthouse
api.query_all("kusto query | take 20")

# Kusto query to ADX
df = api.adxtable2df(api.adx_query("kusto query | take 20"))

# General azure cli cmd
api.azcli(["config", "set", "extension.use_dynamic_install=yes_without_prompt"])
rules = api.azcli(["sentinel", "alert-rule", "list", "-g", resource_group, "-w", workspace, "--subscription", subscription])
```

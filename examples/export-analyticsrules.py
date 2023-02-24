# ---
# jupyter:
#   jupytext:
#     text_representation:
#       extension: .py
#       format_name: light
#       format_version: '1.5'
#       jupytext_version: 1.14.4
#   kernelspec:
#     display_name: Python 3 (ipykernel)
#     language: python
#     name: python3
# ---

# + tags=[]
from squ import api
import json

api.azcli(["config", "set", "extension.use_dynamic_install=yes_without_prompt"])

# + tags=[]
resource_group = "..."
workspace = "..."
subscription = "..."

rules = api.azcli(
    [
        "sentinel",
        "alert-rule",
        "list",
        "-g",
        resource_group,
        "-w",
        workspace,
        "--subscription",
        subscription,
    ]
)
template_rules = []
for rule in rules:
    if rule["name"] == "BuiltInFusion":  # skip fusion rule
        continue
    template_rule = {
        "name": f"[concat(parameters('Workspace'),'/Microsoft.SecurityInsights/{rule['name']}')]",
        "type": "Microsoft.OperationalInsights/workspaces/providers/alertRules",
        "kind": rule["kind"],
        "apiVersion": "2022-09-01-preview",
        "properties": rule.copy(),
    }
    for param in [
        "id",
        "etag",
        "name",
        "type",
        "kind",
        "resourceGroup",
        "lastModifiedUtc",
    ]:
        template_rule["properties"].pop(param)
    template_rules.append(template_rule)

# + tags=[]
armtemplate = {
    "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
    "contentVersion": "1.0.0.0",
    "parameters": {
        "Workspace": {
            "type": "string",
            "metadata": {
                "description": "The Microsoft Sentinel workspace into which the ASIM functions and Analytics Rules will be deployed. Has to be in the selected Resource Group."
            },
        },
        "WorkspaceRegion": {
            "type": "string",
            "defaultValue": "[resourceGroup().location]",
            "metadata": {
                "description": "The region of the selected workspace. The default value will use the Region selection above (this shouldn't need to be changed)."
            },
        },
    },
    "resources": [
        {
            "type": "Microsoft.Resources/deployments",
            "apiVersion": "2021-04-01",
            "name": "ASimFullDeployment",
            "properties": {
                "mode": "Incremental",
                "templateLink": {
                    "uri": "https://raw.githubusercontent.com/Azure/Azure-Sentinel/master/ASIM/ASimFullDeployment.json",
                    "contentVersion": "1.0.0.0",
                },
                "parameters": {
                    "Workspace": {"value": "[parameters('Workspace')]"},
                    "WorkspaceRegion": {"value": "[parameters('WorkspaceRegion')]"},
                },
            },
        },
        {
            "type": "Microsoft.Resources/deployments",
            "apiVersion": "2021-04-01",
            "name": "AnalyticsRules",
            "dependsOn": ["ASimFullDeployment"],
            "properties": {
                "mode": "Incremental",
                "template": {
                    "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
                    "contentVersion": "1.0.0.0",
                    "resources": template_rules,
                },
            },
        },
    ],
    "outputs": {},
}
open("analyticsrules-deployment.json", "w").write(
    json.dumps(armtemplate, indent=2)
)

"""AX semantics mapped to owned main-document DOM nodes; see THIRD_PARTY_NOTICES.md."""

import json

_ROLES = {
    "button",
    "link",
    "checkbox",
    "radio",
    "switch",
    "tab",
    "menuitem",
    "option",
    "combobox",
    "textbox",
    "searchbox",
    "spinbutton",
    "gridcell",
}
_SCOPES = {"dialog", "alertdialog", "form", "region", "group", "listbox", "grid", "table"}


def prepare_accessibility(transport) -> dict:
    if not hasattr(transport, "_cdp"):
        return {"source": "dom-fallback", "reason": "accessibility transport unavailable"}
    try:
        nodes = transport._cdp("Accessibility.getFullAXTree").get("nodes", [])
    except (OSError, RuntimeError):
        transport.evaluate_js("if(window.__jevFast) window.__jevFast.axRecords=[]")
        return {"source": "dom-fallback", "reason": "accessibility tree unavailable"}
    if not nodes:
        transport.evaluate_js("if(window.__jevFast) window.__jevFast.axRecords=[]")
        return {"source": "dom-fallback", "reason": "empty accessibility tree"}
    by_id = {node["nodeId"]: node for node in nodes}
    parents = {child: node["nodeId"] for node in nodes for child in node.get("childIds", [])}
    records = []
    for node in nodes:
        role = node.get("role", {}).get("value")
        if node.get("ignored") or role not in _ROLES or not node.get("backendDOMNodeId"):
            continue
        context = []
        parent = node.get("parentId", parents.get(node["nodeId"]))
        seen = {node["nodeId"]}
        while parent and parent not in seen:
            seen.add(parent)
            ancestor = by_id.get(parent, {})
            ancestor_role = ancestor.get("role", {}).get("value")
            if not ancestor.get("ignored") and ancestor_role in _SCOPES:
                context.append({"role": ancestor_role, "label": ancestor.get("name", {}).get("value", "")[:160]})
                if len(context) == 4:
                    break
            parent = ancestor.get("parentId", parents.get(parent))
        records.append(
            {
                "backend_node_id": node["backendDOMNodeId"],
                "role": role,
                "label": node.get("name", {}).get("value", "")[:500],
                "context": list(reversed(context)),
                "states": {
                    prop["name"]: prop.get("value", {}).get("value")
                    for prop in node.get("properties", [])
                    if prop["name"]
                    in {"checked", "selected", "expanded", "disabled", "pressed", "multiline", "readonly"}
                },
            }
        )
    omitted = max(0, len(records) - 500)
    records = records[:500]
    known = set(
        transport.evaluate_js("""(() => {
      const c=window.__jevFast ||= {ids:new WeakMap(),nodes:new Map(),next:1};
      c.axNodes ||= new Map();
      for(const [id,e] of c.axNodes) if(!e.isConnected) c.axNodes.delete(id);
      return [...c.axNodes.keys()];
    })()""")
        or []
    )
    mapped = set(known)
    try:
        for record in records:
            node_id = record["backend_node_id"]
            if node_id in known:
                continue
            try:
                remote = transport._cdp("DOM.resolveNode", backendNodeId=node_id, objectGroup="jev-accessibility")
                object_id = remote.get("object", {}).get("objectId")
                if not object_id:
                    continue
                result = transport._cdp(
                    "Runtime.callFunctionOn",
                    objectId=object_id,
                    returnByValue=True,
                    functionDeclaration="function(id){if(this.ownerDocument!==document||this.getRootNode()!==document||!this.isConnected||this.nodeType!==1)return false;window.__jevFast.axNodes.set(id,this);return true;}",
                    arguments=[{"value": node_id}],
                )
                if result.get("result", {}).get("value"):
                    mapped.add(node_id)
            except (OSError, RuntimeError):
                continue
    finally:
        transport._cdp("Runtime.releaseObjectGroup", objectGroup="jev-accessibility")
    transport.evaluate_js("window.__jevFast.axRecords=" + json.dumps(records))
    return {
        "source": "accessibility",
        "ax_nodes": len(nodes),
        "ax_omitted_controls": omitted,
        "unmapped_controls": sum(record["backend_node_id"] not in mapped for record in records),
    }

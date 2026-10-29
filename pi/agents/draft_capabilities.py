"""What a tool draft is built from, and the finished draft in plain steps.

The drafting model reads every capability it is offered, and a small local model reads about 20
tokens a second, so it gets only the tools and published workflows that share words with the
owner's description (at most 8, words few tools have counting more), each in a few words. This
was the command line's selection; it lives here so the chat, the Tools screen and the command line
draft from the same list. Published workflows are building blocks like any tool.

The steps are Pi's own reading of the finished document, one line per step, in the order the
steps run. No model writes them, so they say what the document does, not what a model claims.
"""

from __future__ import annotations

import re

TOOLS = 8
CAPABILITIES = 200  # the most a draft request takes (`workflow_drafts.Request`)
# Conker's own tools file requests in Pi; a workflow cannot call them.
OWN = "conker."
WORKFLOW = re.compile(r"^workflow:([a-z0-9][a-z0-9.-]{1,79}):([1-9][0-9]{0,9}):[a-f0-9]{64}$")
STOP = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "that",
    "this",
    "then",
    "into",
    "when",
    "what",
    "your",
    "you",
    "are",
    "was",
    "its",
    "all",
    "any",
    "get",
    "give",
    "gives",
    "return",
    "returns",
    "find",
    "found",
    "list",
    "show",
    "tell",
    "make",
    "use",
    "using",
    "each",
    "every",
    "about",
}
OPERATORS = {
    "add": "plus",
    "subtract": "minus",
    "multiply": "times",
    "divide": "divided by",
    "equals": "equals",
    "greater": "is more than",
    "less": "is less than",
    "contains": "contains",
}


def available(tools) -> list[dict]:
    """Tools Conker can use, as draft capabilities. A published workflow
    (`workflow:<id>:<version>:<digest>`) becomes a building block by its id and version."""
    found = []
    for tool in tools:
        identity = str(tool.id)
        if identity.startswith(OWN):
            continue
        published = WORKFLOW.fullmatch(identity)
        found.append(
            {
                "id": published.group(1) if published else identity,
                "kind": "workflow" if published else "tool",
                "name": str(tool.name or identity)[:160],
                "description": str(tool.description or "")[:800],
                "inputs": [
                    {
                        "name": str(field.get("name"))[:200],
                        "type": str(field.get("type") or "string")[:40],
                        "required": field.get("required") is True,
                        "description": str(field.get("description") or "")[:500],
                    }
                    for field in (tool.inputs or [])[:100]
                    if isinstance(field, dict) and field.get("name")
                ],
                "version": int(published.group(2)) if published else 1,
                "authorization": str(getattr(tool, "authorization", "") or "auto"),
            }
        )
    return found[:CAPABILITIES]


def words(text: str) -> set[str]:
    """Lowercase word stems: `disks` and `disk` match, short and common words do not count."""
    found = set()
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        word = word[:-1] if len(word) > 4 and word.endswith("s") else word
        if len(word) >= 3 and word not in STOP:
            found.add(word)
    return found


def relevant(capabilities: list[dict], text: str, limit: int = TOOLS) -> list[dict]:
    """The capabilities whose id, name, description or inputs share words with the request, best
    first. When nothing matches, every capability is offered."""
    wanted = words(text)
    vocabulary = [
        words(" ".join([item["id"], item["name"], item["description"]]))
        | words(" ".join(field["name"] for field in item["inputs"]))
        for item in capabilities
    ]
    # A word most tools share ("search") says less than one only a few have ("web").
    spread = {word: sum(word in own for own in vocabulary) for word in wanted}
    scored = []
    for index, (item, own) in enumerate(zip(capabilities, vocabulary, strict=True)):
        if shared := wanted & own:
            scored.append((-sum(1 / spread[word] for word in shared), index, item))
    return [item for *_, item in sorted(scored)[:limit]] or capabilities[:CAPABILITIES]


def compact(item: dict) -> dict:
    """What the model needs from a capability, in fewer words; never its authorization."""
    return {
        "id": item["id"],
        "kind": item["kind"],
        "name": item["name"],
        "description": item["description"][:240],
        "inputs": [
            {
                **{key: field[key] for key in ("name", "type", "required") if key in field},
                "description": str(field.get("description", ""))[:100],
            }
            for field in item["inputs"]
        ],
        "version": item["version"],
    }


def _order(document: dict) -> list[dict]:
    """The steps from the input step along the connections, then any step no path reaches."""
    nodes = {node["id"]: node for node in document["nodes"]}
    following: dict[str, list[str]] = {}
    for edge in document["edges"]:
        following.setdefault(edge["source"], []).append(edge["target"])
    queue = [node["id"] for node in document["nodes"] if node["type"] == "input"][:1]
    seen: list[str] = []
    while queue:
        current = queue.pop(0)
        if current in seen or current not in nodes:
            continue
        seen.append(current)
        queue.extend(following.get(current, []))
    seen += [identity for identity in nodes if identity not in seen]
    return [nodes[identity] for identity in seen]


def _quoted(text: str) -> str:
    flat = " ".join(str(text).split())
    return f"“{flat[:60]}…”" if len(flat) > 60 else f"“{flat}”"


def _value(value, labels: dict[str, str]) -> str:
    """One argument in words: where it comes from, or the value itself."""
    if isinstance(value, str):
        if value == "$last":
            return "the previous step's result"
        if value.startswith("$input."):
            return "the " + value[len("$input.") :][:60]
        if value.startswith("$steps."):
            step, _, field = value[len("$steps.") :].partition(".")
            source = _quoted(labels.get(step, step))
            return f"the {field[:60]} from {source}" if field else f"the result of {source}"
        return _quoted(value.replace("$$", "$"))
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return str(value)
    return "a list" if isinstance(value, list) else "a set of values" if value else "nothing"


def _step(node: dict, labels: dict[str, str], names: dict, asks: set, inputs: list) -> str:
    kind, config, label = node["type"], node.get("config") or {}, labels[node["id"]]
    if kind == "input":
        named = [f"the {field['name']}" for field in inputs]
        if not named:
            return "Start"
        listed = named[0] if len(named) == 1 else ", ".join(named[:-1]) + " and " + named[-1]
        return f"Start with {listed}"
    if kind in ("tool_call", "workflow_call"):
        target = config.get("tool") if kind == "tool_call" else config.get("toolId")
        line = f"{label} (uses {names.get(target, str(target)[:80])}"
        return line + (", asks you first)" if target in asks else ")")
    if kind == "calculation":
        left, right = _value(config.get("left"), labels), _value(config.get("right"), labels)
        return f"Work out {left} {OPERATORS.get(config.get('operator'), '?')} {right}"
    if kind == "condition":
        left = _value(config.get("left"), labels)
        if config.get("operator") == "exists":
            return f"Check whether {left} exists"
        right = _value(config.get("right"), labels)
        return f"Check whether {left} {OPERATORS.get(config.get('operator'), '?')} {right}"
    if kind == "loop":
        items = _value(config.get("items"), labels)
        return f"Go through each item in {items}, at most {config.get('limit', 20)}"
    if kind == "set":
        return f"{label}: set {_value(config.get('value'), labels)}"
    if "value" not in config:
        return "Finish"
    return f"Give back {_value(config.get('value'), labels)}"


def steps(document: dict, capabilities: list[dict]) -> list[str]:
    """The draft in plain words, one line per step, in the order the steps run."""
    names = {item["id"]: item["name"] for item in capabilities}
    asks = {item["id"] for item in capabilities if item.get("authorization", "auto") != "auto"}
    labels = {node["id"]: node.get("label") or node["type"] for node in document["nodes"]}
    return [
        _step(node, labels, names, asks, document.get("inputs") or [])[:300]
        for node in _order(document)
    ]

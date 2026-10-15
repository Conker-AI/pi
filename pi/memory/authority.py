"""Operator-owned agent-to-namespace bindings; credentials never come from chat."""

import json
import re


def clients(raw, environment, factory):
    """Resolve explicit read-only clients; no fallback to Companion credentials."""
    if not raw:
        return {}
    result = {}
    try:

        def pairs(items):
            value = {}
            for key, item in items:
                if key in value:
                    raise ValueError()
                value[key] = item
            return value

        value = json.loads(raw, object_pairs_hook=pairs)
        if not isinstance(value, dict) or len(value) > 100:
            raise ValueError()
        definitions = []
        for agent, binding in value.items():
            if (
                not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", agent)
                or agent == "companion"
                or not isinstance(binding, dict)
                or set(binding) != {"namespace", "keyEnv"}
            ):
                raise ValueError()
            namespace, key_env = binding["namespace"], binding["keyEnv"]
            if (
                not isinstance(namespace, str)
                or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", namespace)
                or not isinstance(key_env, str)
                or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,100}", key_env)
            ):
                raise ValueError()
            secret = environment.get(key_env, "").strip()
            if not secret:
                raise ValueError()
            definitions.append((agent, namespace, secret))
        for agent, namespace, secret in definitions:
            result[agent] = factory(namespace, secret)
        return result
    except Exception:
        for client in result.values():
            client.close()
        raise ValueError("Invalid specialist memory authority configuration.") from None

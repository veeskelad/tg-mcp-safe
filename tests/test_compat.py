"""Схема инструментов совпадает со старым сервером (эталон tests/fixtures/tools.json)."""
import asyncio
import json
from pathlib import Path

import tg_mcp_safe.tools  # noqa: F401
from tg_mcp_safe.app import mcp

REF = {t["name"]: t for t in json.loads((Path(__file__).parent / "fixtures" / "tools.json").read_text())}


def _norm(schema):
    props = schema.get("properties", {})
    out = {}
    for k, v in props.items():
        types = sorted({x.get("type", "any") for x in v.get("anyOf", [v])})
        out[k] = (tuple(types), json.dumps(v.get("default", "<none>"), sort_keys=True))
    return out, sorted(schema.get("required", []))


def _ours():
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


def test_same_names():
    ours = set(_ours())
    assert ours == set(REF), {"missing": sorted(set(REF) - ours), "extra": sorted(ours - set(REF))}


def test_same_params():
    ours = _ours()
    bad = {}
    for name, ref in REF.items():
        if name not in ours:
            continue
        a, b = _norm(ours[name].inputSchema), _norm(ref["inputSchema"])
        if a != b:
            bad[name] = {"ours": a, "ref": b}
    assert not bad, json.dumps(bad, ensure_ascii=False, indent=1)


def test_same_readonly():
    ours = _ours()
    bad = [n for n, r in REF.items() if n in ours and bool((r["annotations"] or {}).get("readOnlyHint")) != bool(ours[n].annotations.readOnlyHint)]
    assert not bad, bad

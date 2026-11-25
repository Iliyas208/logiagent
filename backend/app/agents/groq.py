"""Free alternative to Claude: Groq's OpenAI-compatible Chat Completions API (e.g. openai/gpt-oss-120b).

Same agents, same tools; only the transport differs. Used when GROQ_API_KEY is set and there is no
ANTHROPIC_API_KEY, or when AGENT_MODE=groq.
"""
import json
import os

import httpx

from .. import config

URL = "https://api.groq.com/openai/v1/chat/completions"


class GroqError(Exception):
    pass


def _post(body: dict) -> dict:
    # Groq sits behind Cloudflare, which rejects some default client user agents.
    headers = {"authorization": "Bearer " + os.getenv("GROQ_API_KEY", ""), "user-agent": "logiagent/0.2"}
    try:
        r = httpx.post(URL, json={"model": config.groq_model(), **body}, headers=headers, timeout=60)
    except httpx.HTTPError as e:
        raise GroqError(f"network: {e}") from None
    if r.status_code != 200:
        raise GroqError(f"HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


def extract(system: str, user: str, schema: dict) -> dict:
    """JSON extraction. The schema goes into the prompt; json_object mode guarantees valid JSON."""
    prompt = system + "\n\nВерни только JSON-объект с ключами строго по этой JSON-схеме:\n" + json.dumps(schema, ensure_ascii=False)
    out = _post({"temperature": 0, "response_format": {"type": "json_object"},
                 "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": user}]})
    try:
        return json.loads(out["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise GroqError(f"bad response: {e}") from None


def tools_spec(tools: list) -> list:
    """Convert Anthropic-style tool definitions to the OpenAI function format."""
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": t["input_schema"]}} for t in tools]


def tool_loop(system: str, messages: list, tools: list, run_tool, max_steps: int) -> str:
    """Let the model call tools until it answers. run_tool(name, args) -> str. Returns the final text."""
    msgs = [{"role": "system", "content": system}] + messages
    spec = tools_spec(tools)
    for _ in range(max_steps):
        out = _post({"temperature": 0, "messages": msgs, "tools": spec})
        try:
            msg = out["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise GroqError(f"bad response: {e}") from None
        calls = msg.get("tool_calls") or []
        if not calls:
            return (msg.get("content") or "").strip()
        msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for call in calls:
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except ValueError:
                args = {}
            msgs.append({"role": "tool", "tool_call_id": call["id"], "content": run_tool(call["function"]["name"], args)})
    return ""

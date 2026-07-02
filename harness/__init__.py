"""ECHARA harness — gives a raw API model filesystem access via a tool loop.

The M2 path (providers/{claude_code,codex}) wraps CLI agents that already own
their tool loop. This package is the opposite: for providers that expose only a
raw chat-completions endpoint (Cerebras, etc.), WE own the loop — send tool
schemas, execute the model's tool calls against a clamped workspace, feed
results back, repeat until it stops. Ported in spirit from opencode's
session/tool layer; see M2_5_OPENCODE_MAP.md.
"""

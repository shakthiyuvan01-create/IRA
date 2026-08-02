"""
IRA Forge — AI-powered tool creation system.

The forge lets IRA create new Python tools at runtime via an LLM pipeline:
1. Plan  — AI drafts a tool plan
2. Codegen — AI writes the tool code
3. Verify — Ephemeral venv sandbox verifies the tool
4. Fix  — AI fixes any issues (up to N retries)
5. Install — Tool is registered in the runtime

Sub-packages:
- forge/debug_log.py         — Structured debug logging
- forge/prompts_config.py    — Loadable prompt configuration
- forge/secrets_config.py    — API key secrets management
- forge/forge_routing.py     — Codegen profile routing
- forge/runtime_client.py    — Tool runtime HTTP client
- forge/litellm_client.py    — Unified streaming LLM client
- forge/tools_engine.py      — Dynamic tool loading, execution, manifest
- forge/tool_verify.py       — Ephemeral venv verification
- forge/tool_creator.py      — AI code generation pipeline
- forge/build_pipeline.py    — Shared build/install pipeline
- forge/build_ui_qa.py       — Interactive skill UI QA
- forge/forge_batch.py       — Multi-tool batch forging
- forge/tool_build_stream.py — Streaming tool build
"""

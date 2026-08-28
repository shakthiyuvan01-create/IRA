# IRA Plugin System (ported from Brahma AI)

Plugins live in this directory. Drop a `.py` file here and IRA loads it at boot.

## Contract

A plugin module can either:

1. export a `plugin` object (instance/class), or
2. just define hook functions directly (the module itself is used).

## Hooks

IRA calls these hooks on every loaded plugin:

| Hook | Signature | When |
|------|-----------|------|
| `on_brahma_created(brahma)` | once at boot, after loading | register state, wire callbacks |
| `on_command(text)` | on every typed/voice command | return `True` to consume the command |

Example:

```python
def on_command(text: str) -> bool:
    if text.strip().lower().startswith("hello plugin"):
        print("[Plugin] hello from my plugin")
        return True
    return False
```

If any plugin returns `True` from `on_command`, IRA treats the command as
handled and does not send it to the model.

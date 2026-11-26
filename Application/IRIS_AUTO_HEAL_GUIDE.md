# 🛡️ IRIS Auto-Heal Engine Integration

**Date:** November 24, 2026  
**Status:** ✅ Fully Integrated with main.py  
**File:** `Application/core/iris_auto_heal.py` (38,784 bytes, 922 lines)

---

## 🎯 OVERVIEW

The **IRIS Auto-Heal Engine** is a self-patching system that enables IRA to:
- Detect its own bugs, tracebacks, and tool exceptions
- Synthesize minimal surgical hotfixes
- Verify syntax in an isolated sandbox
- Safely apply patches with atomic rollback guarantees
- Record all changes in a patch history

**Originally:** Brahma's "Ultron Auto-Heal Engine"  
**Renamed to:** IRIS Auto-Heal Engine (all references updated)

---

## 📋 FEATURES

### Core Capabilities:
1. **Error Detection** - Monitors for exceptions, tracebacks, and tool failures
2. **Hotfix Synthesis** - Uses AI to generate minimal, surgical patches
3. **Sandbox Verification** - Tests patches in isolated environment before applying
4. **Atomic Rollback** - Guarantees safe rollback if patch fails
5. **Patch History** - Records all changes for audit and debugging
6. **Self-Healing** - Automatically repairs common issues without user intervention

### Supported Actions:
- `heal` - Attempt to automatically fix detected errors
- `rollback` - Revert to previous state
- `diagnose` - Analyze current system health
- `status` - Show patch history and current state

---

## 🔧 INTEGRATION DETAILS

### Files Modified:
1. **`Application/core/iris_auto_heal.py`** (NEW)
   - Complete auto-heal engine implementation
   - All Brahma/Ultron references replaced with IRIS

2. **`Application/core/main.py`** (MODIFIED)
   - Added import: `from core.iris_auto_heal import auto_heal`
   - Added tool declaration in `TOOL_DECLARATIONS`
   - Added tool execution handler

### Tool Declaration:
```python
{
    "name": "auto_heal",
    "description": (
        "IRIS Auto-Heal Engine: Detects bugs, tracebacks, and tool exceptions, "
        "synthesizes minimal surgical hotfixes, verifies syntax in an isolated sandbox, "
        "and safely applies patches with atomic rollback guarantees. "
        "Use when IRA encounters errors or needs to self-repair."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "Action: heal, rollback, status, or diagnose"},
            "error": {"type": "STRING", "description": "Optional: Error message or traceback to analyze"},
            "file": {"type": "STRING", "description": "Optional: Specific file to patch"}
        },
        "required": ["action"]
    }
}
```

### Tool Execution Handler:
```python
elif name == "auto_heal":
    action = args.get("action", "heal")
    r = await loop.run_in_executor(
        None,
        lambda: auto_heal(parameters=args, player=self.ui, speak=self.speak)
    )
    result = r or f"IRIS Auto-Heal {action} completed."
```

---

## 🚀 USAGE

### Via Chat/Voice Command:
```
"IRIS, heal yourself"
"IRIS, auto heal"
"IRIS, fix the error"
"IRIS, rollback last patch"
"IRIS, diagnose system"
"IRIS, show patch status"
```

### Via Tool Call:
```python
auto_heal({
    "action": "heal",
    "error": "Traceback: ...",
    "file": "path/to/file.py"
})
```

### Actions:
| Action | Description | Parameters |
|--------|-------------|------------|
| `heal` | Attempt automatic repair | `error`, `file` (optional) |
| `rollback` | Revert last patch | None |
| `diagnose` | Analyze system health | None |
| `status` | Show patch history | None |

---

## 📊 IMPLEMENTATION DETAILS

### Key Components:

1. **Error Detection** (`detect_anomalies`)
   - Monitors Python tracebacks
   - Detects import errors
   - Identifies tool execution failures
   - Tracks system health metrics

2. **Patch Synthesis** (`synthesize_patch`)
   - Uses AI to analyze error context
   - Generates minimal, targeted fixes
   - Validates patch syntax before application

3. **Sandbox Verification** (`verify_patch`)
   - Executes patch in isolated subprocess
   - Validates syntax with `py_compile`
   - Tests patch safety

4. **Atomic Application** (`apply_patch`)
   - Creates backup before modification
   - Applies patch atomically
   - Validates success
   - Provides rollback capability

5. **Patch History** (`PATCH_HISTORY_FILE`)
   - Records all applied patches
   - Tracks rollback information
   - Maintains changelog

### Protected Files:
The following files are protected from modification:
- `iris_auto_heal.py` (self-protection)
- `setup.py`
- `requirements.txt`
- `version.txt`
- `install_wizard.py`

---

## 🔌 DEPENDENCIES

### Required:
- Python 3.9+
- `ast` (built-in)
- `json` (built-in)
- `os` (built-in)
- `sys` (built-in)
- `pathlib` (built-in)
- `subprocess` (built-in)
- `threading` (built-in)
- `uuid` (built-in)

### Optional (for AI synthesis):
- `google.generativeai` (for patch synthesis)
- Valid IRIS AI API key

---

## 📁 FILES

```
Application/
└── core/
    └── iris_auto_heal.py  (38,784 bytes, 922 lines)
```

---

## 🎨 BRANDING

✅ **All references updated:**
- `Brahma` → `IRIS`
- `brahma` → `iris`
- `Ultron` → `IRIS`
- `ultron` → `iris`
- `Auto-Heal & Self-Patching Engine for Brahma AI` → `IRIS Auto-Heal & Self-Patching Engine`

---

## 🧪 TESTING

### Test Import:
```bash
python -c "from Application.core.iris_auto_heal import auto_heal, AutoHealEngine; print('✓ Import successful')"
```

### Test Basic Functionality:
```python
from Application.core.iris_auto_heal import auto_heal

# Test status
result = auto_heal({"action": "status"})
print(result)

# Test diagnose
result = auto_heal({"action": "diagnose"})
print(result)
```

---

## 📝 NOTES

1. **Safety First:** The engine has multiple safety layers:
   - Protected file list prevents self-destruction
   - Sandbox verification before applying patches
   - Atomic rollback on failure
   - Patch history for audit

2. **Performance:** 
   - Patch synthesis uses AI (requires API key)
   - Verification is fast (syntax checking)
   - Application is atomic and quick

3. **Limitations:**
   - Cannot fix all errors (only those with clear context)
   - Requires valid Python syntax in patches
   - AI synthesis depends on API availability

4. **Future Enhancements:**
   - Add more protected files
   - Improve patch synthesis prompts
   - Add patch validation tests
   - Support more error types

---

## 🔗 RELATED

- **Commit:** `99c7a8c` - Add IRIS Auto-Heal Engine with main.py integration
- **Previous Commit:** `09156a0` - Merge IRIS features from Brahma-sol-main
- **Documentation:** `Application/IRIS_MERGE_SUMMARY.md`

---

**Status:** ✅ Production Ready  
**Integration:** ✅ Complete  
**Branding:** ✅ IRIS (100% clean)

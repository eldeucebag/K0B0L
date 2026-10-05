#!/usr/bin/env bash
# Run the chat test suite.
#
#   ./run_all.sh           everything
#   ./run_all.sh --quick   only the suite that needs no model
#
# The three model-backed suites talk to Ollama at CHAT_TEST_URL (default
# http://127.0.0.1:11436, the flash-attention + q4_0 instance from
# ../rt-fa-server.sh) and need the attacker model pulled. Override with
# CHAT_TEST_URL / CHAT_TEST_MODEL.
set -uo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
tests=(test_tools.py test_verdict.py test_recovery.py test_toolloop.py test_compress.py test_memory.py test_selfmem.py test_skills.py test_skills2.py test_skills_editor.py test_runctl.py test_cycle.py test_engine.py test_frontend.py test_pty.py test_fspty.py test_soul.py test_docs.py test_themes.py test_semantic.py test_menubar.py test_sessions.py test_openai_compat.py)
if [ "${1:-}" = "--quick" ]; then
    tests=(test_tools.py test_verdict.py test_recovery.py test_toolloop.py test_compress.py test_memory.py test_selfmem.py test_skills.py test_skills2.py test_skills_editor.py test_cycle.py test_soul.py test_docs.py test_themes.py test_semantic.py test_menubar.py test_sessions.py test_openai_compat.py)
fi

failed=0
for test in "${tests[@]}"; do
    output=$(mktemp)
    if "$PY" "$test" >"$output" 2>&1; then
        printf 'PASS  %-18s %s checks\n' "$test" "$(grep -c '^ok  ' "$output")"
    else
        printf 'FAIL  %-18s %s ok / %s FAIL\n' \
            "$test" "$(grep -c '^ok  ' "$output")" "$(grep -c '^FAIL' "$output")"
        cat "$output"
        failed=1
    fi
    rm -f "$output"
done

if [ "$failed" -eq 0 ]; then
    echo
    echo "all suites passed"
fi
exit "$failed"

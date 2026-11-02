import os
import sys
import tempfile
import time
import json
import shutil

# Offscreen Qt
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ezexp.agent import AGENT_TOOLS, SYSTEM_PROMPT, AgentClient, BridgeWorker

def run_tests():
    print("Testing EzExp Tool Contract v1...")

    # 1. Tools inventory
    tool_names = sorted(t["function"]["name"] for t in AGENT_TOOLS)
    expected_seven = sorted([
        "search_files",
        "list_folder",
        "open_file",
        "create_file",
        "create_folder",
        "read_file",
        "run_python",
    ])
    assert tool_names == expected_seven, f"Expected 7 tools {expected_seven}, got {tool_names}"
    print("[PASS] 7 tools declared in AGENT_TOOLS")

    # Check schemas
    tool_map = {t["function"]["name"]: t["function"] for t in AGENT_TOOLS}
    assert "rel_path" in tool_map["read_file"]["parameters"]["properties"]
    assert tool_map["read_file"]["parameters"]["required"] == ["rel_path"]

    assert "rel_path" in tool_map["run_python"]["parameters"]["properties"]
    assert "timeout_s" in tool_map["run_python"]["parameters"]["properties"]
    assert tool_map["run_python"]["parameters"]["required"] == ["rel_path"]
    print("[PASS] Schemas for read_file and run_python are correct")

    # Check SYSTEM_PROMPT
    assert "read_file(rel_path)" in SYSTEM_PROMPT
    assert "run_python(rel_path" in SYSTEM_PROMPT
    print("[PASS] SYSTEM_PROMPT contains read_file and run_python")

    # Setup temp grant dir
    grant_dir = tempfile.mkdtemp(prefix="ez_test_grant_")
    outside_dir = tempfile.mkdtemp(prefix="ez_test_outside_")

    try:
        client = AgentClient(config={"grant_dir": grant_dir, "api_key": "dummy"})

        # --- read_file tests ---
        # 1. No grant configured
        c_no_grant = AgentClient(config={"grant_dir": ""})
        res = c_no_grant.dispatch_tool("read_file", {"rel_path": "foo.txt"})
        assert "grant_dir is not configured" in res, res

        # 2. Empty rel_path
        res = client.dispatch_tool("read_file", {"rel_path": ""})
        assert "rel_path cannot be empty" in res, res

        # 3. Path escape: absolute path
        res = client.dispatch_tool("read_file", {"rel_path": "C:\\secret.txt"})
        assert "Path escapes granted folder" in res, res

        res = client.dispatch_tool("read_file", {"rel_path": "/etc/passwd"})
        assert "Path escapes granted folder" in res, res

        # 4. Path escape: .. traversal
        res = client.dispatch_tool("read_file", {"rel_path": "../outside.txt"})
        assert "Path escapes granted folder" in res, res

        # 5. File not found
        res = client.dispatch_tool("read_file", {"rel_path": "nonexistent.txt"})
        assert "File not found" in res, res

        # 6. Path is a directory
        sub_dir = os.path.join(grant_dir, "subdir")
        os.makedirs(sub_dir, exist_ok=True)
        res = client.dispatch_tool("read_file", {"rel_path": "subdir"})
        assert "Path is not a file" in res, res

        # 7. Valid text file
        hello_file = os.path.join(grant_dir, "hello.txt")
        with open(hello_file, "w", encoding="utf-8") as f:
            f.write("Hello from EzExp granted folder!\nSecond line.")
        res = client.dispatch_tool("read_file", {"rel_path": "hello.txt"})
        assert res == "Hello from EzExp granted folder!\nSecond line.", res

        # 8. Truncation at 200 KB
        big_file = os.path.join(grant_dir, "big.txt")
        big_content = "X" * 250000  # 250 KB
        with open(big_file, "w", encoding="utf-8") as f:
            f.write(big_content)
        res = client.dispatch_tool("read_file", {"rel_path": "big.txt"})
        assert "[TRUNCATED: file exceeded 200 KB]" in res
        assert len(res) < 250000
        assert res.startswith("X" * (200 * 1024))

        # 9. Binary file (null bytes)
        bin_file = os.path.join(grant_dir, "image.bin")
        with open(bin_file, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
        res = client.dispatch_tool("read_file", {"rel_path": "image.bin"})
        assert "Cannot read binary file" in res, res

        # 10. Binary file (invalid UTF-8 sequence)
        bad_utf8 = os.path.join(grant_dir, "bad.bin")
        with open(bad_utf8, "wb") as f:
            f.write(b"\xff\xfe\xfd\x80\x81\x82")
        res = client.dispatch_tool("read_file", {"rel_path": "bad.bin"})
        assert "Cannot read binary file" in res, res
        print("[PASS] read_file jail, binary detection, and 200 KB truncation passed")

        # --- run_python tests ---
        # 1. No grant configured
        res = c_no_grant.dispatch_tool("run_python", {"rel_path": "foo.py"})
        assert "grant_dir is not configured" in res, res

        # 2. Empty rel_path
        res = client.dispatch_tool("run_python", {"rel_path": ""})
        assert "rel_path cannot be empty" in res, res

        # 3. Path escape: absolute path
        res = client.dispatch_tool("run_python", {"rel_path": "C:\\script.py"})
        assert "Path escapes granted folder" in res, res

        # 4. Path escape: .. traversal
        res = client.dispatch_tool("run_python", {"rel_path": "../outside.py"})
        assert "Path escapes granted folder" in res, res

        # 5. Non-.py file rejected
        sh_file = os.path.join(grant_dir, "script.sh")
        with open(sh_file, "w", encoding="utf-8") as f:
            f.write("echo hacked")
        res = client.dispatch_tool("run_python", {"rel_path": "script.sh"})
        assert "run_python can only execute .py files" in res, res

        # 6. File not found
        res = client.dispatch_tool("run_python", {"rel_path": "missing.py"})
        assert "File not found" in res, res

        # 7. Valid execution with cwd = grant_dir
        py_hello = os.path.join(grant_dir, "hello.py")
        with open(py_hello, "w", encoding="utf-8") as f:
            f.write("import os\nprint('CWD:', os.getcwd())\nprint('RESULT: hello world')\n")
        res = client.dispatch_tool("run_python", {"rel_path": "hello.py"})
        assert "Exit code: 0" in res, res
        assert "RESULT: hello world" in res, res
        # Check cwd was indeed the grant dir
        assert os.path.realpath(grant_dir).lower() in res.lower(), res

        # 8. Script with non-zero exit code and stderr
        py_err = os.path.join(grant_dir, "fail.py")
        with open(py_err, "w", encoding="utf-8") as f:
            f.write("import sys\nsys.stderr.write('Something broke!\\n')\nsys.exit(42)\n")
        res = client.dispatch_tool("run_python", {"rel_path": "fail.py"})
        assert "Exit code: 42" in res, res
        assert "Stderr:" in res, res
        assert "Something broke!" in res, res

        # 9. Output cap at 8 KB
        py_loud = os.path.join(grant_dir, "loud.py")
        with open(py_loud, "w", encoding="utf-8") as f:
            f.write("print('Y' * 15000)\n")
        res = client.dispatch_tool("run_python", {"rel_path": "loud.py"})
        assert "Exit code: 0" in res, res
        assert "[TRUNCATED: stdout exceeded 8 KB]" in res, res
        assert len(res) < 15000

        # 10. Timeout killing process
        py_sleep = os.path.join(grant_dir, "sleep.py")
        with open(py_sleep, "w", encoding="utf-8") as f:
            f.write("import time\ntime.sleep(10)\n")
        t0 = time.perf_counter()
        res = client.dispatch_tool("run_python", {"rel_path": "sleep.py", "timeout_s": 1})
        elapsed = time.perf_counter() - t0
        assert "timed out after 1 seconds" in res, res
        assert elapsed < 5, f"Took too long: {elapsed}"
        print("[PASS] run_python jail, cwd, stdout/stderr, 8 KB truncation, and timeout passed")

    finally:
        shutil.rmtree(grant_dir, ignore_errors=True)
        shutil.rmtree(outside_dir, ignore_errors=True)

    print("\nALL CONTRACT V1 TESTS PASSED SUCCESSFULLY!")

if __name__ == "__main__":
    run_tests()

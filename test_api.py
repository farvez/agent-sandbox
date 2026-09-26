import json
import urllib.request
import urllib.error

BASE_URL = "http://127.0.0.1:8000"
API_KEY = "sb_live_secret_key_123"


def request(method: str, path: str, body: dict = None):
    url = f"{BASE_URL}{path}"
    headers = {
        "X-API-Key": API_KEY,
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    print("1. Checking API Health...")
    health = request("GET", "/healthz")
    print(f"   Status: {health}\n")

    print("2. Creating an Ephemeral Sandbox Session...")
    session = request("POST", "/v1/sessions", {"template": "sandbox-base:latest"})
    session_id = session["session_id"]
    print(f"   Session ID: {session_id}\n")

    try:
        print("3. Executing Python math command inside sandbox...")
        exec_res = request(
            "POST",
            f"/v1/sessions/{session_id}/exec",
            {"command": "python3 -c \"print(2 ** 16)\""},
        )
        print(f"   Output:\n{exec_res['output']}\n")

        print("4. Writing a file 'greeting.py' into the sandbox...")
        write_res = request(
            "POST",
            f"/v1/sessions/{session_id}/write",
            {"path": "greeting.py", "content": "print('Agent API execution successful!')"},
        )
        print(f"   Write Status: {write_res['status']}\n")

        print("5. Running 'greeting.py' inside the container...")
        run_res = request(
            "POST",
            f"/v1/sessions/{session_id}/exec",
            {"command": "python3 greeting.py"},
        )
        print(f"   Output:\n{run_res['output']}\n")

    finally:
        print("6. Tearing down and deleting session...")
        del_res = request("DELETE", f"/v1/sessions/{session_id}")
        print(f"   Cleanup Status: {del_res['status']}")


if __name__ == "__main__":
    main()
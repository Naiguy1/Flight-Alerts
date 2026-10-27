"""CI-only loopback outage test: Caddy errors must not log subscription tokens."""
from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid


def command(*arguments):
    return subprocess.run(list(arguments), check=True, capture_output=True, text=True, timeout=30).stdout


def main():
    source = (Path(__file__).resolve().parents[1] / "docs/Caddyfile").read_text()
    global_options = source[:source.index("\n}\n") + 3]
    synthetic = "synthetic-unsubscribe-private-token"
    name = "flight-proxy-privacy-" + uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix="flight-proxy-privacy-") as folder:
        config = Path(folder) / "Caddyfile"
        config.write_text(global_options + "\nhttp://:8080 {\n reverse_proxy 127.0.0.1:1\n}\n")
        config.chmod(0o644)
        try:
            command("docker", "run", "-d", "--name", name, "--read-only", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges", "-e", "ACME_EMAIL=admin@example.invalid",
                    "-p", "127.0.0.1::8080", "-v", f"{config}:/etc/caddy/Caddyfile:ro", "flight-alerts-proxy:ci")
            port = command("docker", "port", name, "8080/tcp").strip().rsplit(":", 1)[1]
            observed = False
            for _ in range(30):
                request = Request("http://127.0.0.1:" + port + "/subscriptions/unsubscribe?token=" + synthetic,
                                  headers={"Authorization": "Bearer " + synthetic, "Cookie": "private=" + synthetic})
                try:
                    with urlopen(request, timeout=2):
                        pass
                except HTTPError as error:
                    if error.code == 502:
                        observed = True
                        break
                except URLError:
                    time.sleep(0.2)
            if not observed:
                raise RuntimeError("Proxy outage did not exercise a 502 runtime error")
            logs = subprocess.run(["docker", "logs", name], check=True, capture_output=True, text=True, timeout=10)
            combined = logs.stdout + logs.stderr
            if synthetic in combined or '"request":' in combined:
                raise RuntimeError("Proxy error logs exposed a private request object")
            if '"level":"error"' not in combined:
                raise RuntimeError("Proxy runtime-error logging was not exercised")
            print("Proxy outage error logged without URI tokens, cookies or credentials")
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)


if __name__ == "__main__":
    main()

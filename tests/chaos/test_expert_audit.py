"""End-to-end audit of recording, redaction, diffing, replay, and attestation."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from aiohttp import web

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CLI_MODULE = "assay.cli.main"
SECRETS = (
    "my-secret-jwt-token",
    "session_id=abc123secret",
    "admin-secret",
    "super_secret_password",
    "1111222233334444",
)


def _cli_environment() -> dict[str, str]:
    env = os.environ.copy()
    src_path = str(PROJECT_ROOT / "src")
    current_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        src_path if not current_pythonpath else src_path + os.pathsep + current_pythonpath
    )
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


async def _run_cli(*args: str, cwd: Path) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        CLI_MODULE,
        *args,
        cwd=str(cwd),
        env=_cli_environment(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=45)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise
    return int(process.returncode or 0), output.decode("utf-8", errors="replace")


async def _start_target(
    *,
    candidate: bool,
) -> tuple[web.AppRunner, str, dict[str, int]]:
    state = {"active": 0, "max_active": 0}
    activity_lock = asyncio.Lock()

    async def _response(
        payload: dict[str, Any],
        *,
        status: int = 200,
    ) -> web.Response:
        payload["transaction_id"] = str(uuid.uuid4())
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        return web.json_response(
            payload,
            status=status,
            headers={"Cache-Control": "no-store"},
        )

    async def _dispatch(request: web.Request) -> web.Response:
        path = request.path
        if path == "/chaos/bad-gateway":
            return web.Response(status=502, text="Bad Gateway")
        if path == "/chaos/internal":
            return web.Response(status=500, text="Internal Server Error")
        if path == "/chaos/reset":
            if request.transport is not None:
                request.transport.abort()
            return web.Response(status=200, text="connection aborted")
        if path == "/chaos/hang":
            await asyncio.sleep(0.5)
            return web.json_response({"recovered": True})
        if path == "/chaos/healthy":
            return await _response({"healthy": True})
        if path.startswith("/stress/"):
            await asyncio.sleep(0.02)
            return await _response({"stress_path": path})
        if path == "/users/42" and request.method == "GET":
            status = "pending" if candidate else "active"
            return await _response(
                {"user": {"profile": {"id": 42, "role": "admin", "status": status}}}
            )
        if path == "/users" and request.method == "POST":
            payload = await request.json()
            return await _response(
                {"created": True, "username": payload["username"]},
                status=201,
            )
        if path == "/users/42" and request.method == "PUT":
            payload = await request.json()
            return await _response(
                {"updated": True, "id": 42, "display_name": payload["display_name"]}
            )
        if path == "/orders":
            return await _response({"orders": [{"id": "ord-1"}], "limit": 2})
        if path == "/events":
            payload = await request.json()
            return await _response({"accepted": True, "event": payload["event"]}, status=202)
        if path == "/secrets":
            return await _response(await request.json())
        return web.json_response({"error": "not found"}, status=404)

    async def _handler(request: web.Request) -> web.Response:
        async with activity_lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        try:
            return await _dispatch(request)
        finally:
            async with activity_lock:
                state["active"] -= 1

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", _handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None and site._server.sockets
    port = int(site._server.sockets[0].getsockname()[1])
    return runner, f"http://127.0.0.1:{port}", state


async def _wait_for_port(host: str, port: int, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=0.25,
            )
        except (OSError, asyncio.TimeoutError):
            await asyncio.sleep(0.05)
            continue
        writer.close()
        await writer.wait_closed()
        return
    raise TimeoutError(f"Proxy did not start listening on {host}:{port}.")


async def _stop_recording_process(
    process: asyncio.subprocess.Process,
) -> tuple[int, str]:
    if process.returncode is None:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.send_signal(signal.SIGINT)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=15)
    except asyncio.TimeoutError:
        process.terminate()
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
            output, _ = await process.communicate()
        raise AssertionError("Recording process did not stop gracefully and flush.")
    return int(process.returncode or 0), output.decode("utf-8", errors="replace")


def _recorded_interaction(interaction_id: str, path: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "interaction_id": interaction_id,
        "recorded_at": now,
        "request": {
            "method": "GET",
            "url": f"http://recorded.invalid{path}",
            "path": path,
            "query_params": {},
            "headers": [{"name": "Content-Type", "value": "application/json"}],
            "body": None,
            "content_type": None,
        },
        "response": {
            "status_code": 200,
            "headers": [{"name": "Content-Type", "value": "application/json"}],
            "body": '{"baseline":true}',
            "content_type": "application/json",
            "elapsed_ms": 1.0,
        },
    }


def _write_session(
    workspace: Path,
    session_id: str,
    target_url: str,
    paths: list[str],
) -> Path:
    session_path = workspace / ".assay" / "sessions" / f"session_{session_id}.json"
    payload = {
        "version": "1.0.0",
        "session_id": session_id,
        "target_url": target_url,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "interactions": [
            _recorded_interaction(f"{session_id}-{index}", path) for index, path in enumerate(paths)
        ],
    }
    session_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return session_path


def _load_replay_report(
    workspace: Path,
    session_id: str,
    target_url: str,
) -> dict[str, Any]:
    for report_path in (workspace / ".assay" / "replays").glob("replay_*.json"):
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report["session_id"] == session_id and report["target_url"] == target_url:
            return report
    raise AssertionError(f"No replay report found for {session_id} against {target_url}.")


@pytest.mark.asyncio
async def test_expert_audit_all_modules_and_failure_modes(tmp_path: Path) -> None:
    """Run isolated live-server workflows and assert every audit phase."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target_runners: list[web.AppRunner] = []
    recording_process: asyncio.subprocess.Process | None = None

    try:
        # Phase 1: Initialize, record five representative requests, and SIGINT flush.
        init_code, init_output = await _run_cli("init", cwd=workspace)
        assert init_code == 0, init_output
        assert (workspace / ".assay.yaml").is_file()
        assert (workspace / ".assay" / "keys" / "private_key.pem").is_file()
        assert (workspace / ".assay" / "keys" / "public_key.hex").is_file()
        config_path = workspace / ".assay.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["network"]["timeout_seconds"] = 0.15
        config_path.write_text(
            yaml.safe_dump(config, sort_keys=False),
            encoding="utf-8",
        )

        baseline_runner, baseline_url, baseline_state = await _start_target(candidate=False)
        candidate_runner, candidate_url, candidate_state = await _start_target(candidate=True)
        target_runners.extend((baseline_runner, candidate_runner))

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            proxy_port = int(sock.getsockname()[1])
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        recording_process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            CLI_MODULE,
            "record",
            "--target",
            baseline_url,
            "--port",
            str(proxy_port),
            "--session",
            "expert-audit",
            cwd=str(workspace),
            env=_cli_environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            creationflags=creationflags,
        )
        await _wait_for_port("127.0.0.1", proxy_port)
        proxy_url = f"http://127.0.0.1:{proxy_port}"

        async with httpx.AsyncClient(base_url=proxy_url, timeout=5.0) as client:
            responses = [
                await client.get(
                    "/users/42?include=profile",
                    headers={"X-Trace-Id": "trace-42"},
                ),
                await client.post("/users", json={"username": "nandan"}),
                await client.put(
                    "/users/42",
                    json={"display_name": "Nandan"},
                ),
                await client.get("/orders?limit=2"),
                await client.post("/events", json={"event": "order.created"}),
            ]
            assert [response.status_code for response in responses] == [
                200,
                201,
                200,
                200,
                202,
            ]
            assert responses[0].json()["user"]["profile"] == {
                "id": 42,
                "role": "admin",
                "status": "active",
            }
            assert responses[0].headers["cache-control"] == "no-store"
            assert responses[0].headers["content-type"].startswith("application/json")

            # Phase 2: Exercise secrets through the same active recorder.
            secret_response = await client.post(
                "/secrets",
                headers={
                    "Authorization": "Bearer my-secret-jwt-token",
                    "Cookie": "session_id=abc123secret",
                    "X-API-Key": "admin-secret",
                },
                json={
                    "username": "nandan",
                    "password": "super_secret_password",
                    "card_details": {
                        "cvv": 999,
                        "number": "1111222233334444",
                    },
                },
            )
            assert secret_response.status_code == 200
            secret_response_body = secret_response.json()
            assert secret_response_body["username"] == "nandan"
            assert secret_response_body["password"] == "super_secret_password"
            assert secret_response_body["card_details"] == {
                "cvv": 999,
                "number": "1111222233334444",
            }

        assert recording_process.returncode is None
        record_exit, record_output = await _stop_recording_process(recording_process)
        recording_process = None
        assert record_exit == 0, record_output

        session_path = workspace / ".assay" / "sessions" / "session_expert-audit.json"
        assert session_path.is_file()
        session_data = json.loads(session_path.read_text(encoding="utf-8"))
        interactions = session_data["interactions"]
        assert len(interactions) == 6
        assert [item["request"]["method"] for item in interactions] == [
            "GET",
            "POST",
            "PUT",
            "GET",
            "POST",
            "POST",
        ]
        assert [item["response"]["status_code"] for item in interactions] == [
            200,
            201,
            200,
            200,
            202,
            200,
        ]
        assert interactions[0]["request"]["query_params"] == {"include": "profile"}
        first_request_headers = {
            header["name"].lower(): header["value"]
            for header in interactions[0]["request"]["headers"]
        }
        assert first_request_headers["x-trace-id"] == "trace-42"
        assert json.loads(interactions[1]["request"]["body"]) == {"username": "nandan"}
        assert json.loads(interactions[2]["request"]["body"]) == {"display_name": "Nandan"}
        assert json.loads(interactions[4]["request"]["body"]) == {"event": "order.created"}
        assert json.loads(interactions[0]["response"]["body"])["user"]["profile"]["id"] == 42

        # Phase 2 continued: The session must contain redacted values, not plaintext.
        session_text = session_path.read_text(encoding="utf-8")
        for secret in SECRETS:
            assert secret not in session_text
        secret_interaction = interactions[-1]
        request_headers = {
            header["name"].lower(): header["value"]
            for header in secret_interaction["request"]["headers"]
        }
        for header_name in ("authorization", "cookie", "x-api-key"):
            assert request_headers[header_name] == "[REDACTED]"
        recorded_request_body = json.loads(secret_interaction["request"]["body"])
        assert recorded_request_body["username"] == "nandan"
        assert recorded_request_body["password"] == "[REDACTED]"
        assert recorded_request_body["card_details"] == {
            "cvv": "[REDACTED]",
            "number": "[REDACTED]",
        }
        recorded_response_body = json.loads(secret_interaction["response"]["body"])
        assert recorded_response_body["username"] == "nandan"
        assert recorded_response_body["password"] == "[REDACTED]"
        assert recorded_response_body["card_details"] == {
            "cvv": "[REDACTED]",
            "number": "[REDACTED]",
        }

        # Phase 3: Run the real CLI with two repeatable volatile-field exclusions.
        verify_code, verify_output = await _run_cli(
            "verify",
            "--recording",
            str(session_path),
            "--baseline",
            baseline_url,
            "--candidate",
            candidate_url,
            "--ignore-path",
            "$.transaction_id",
            "--ignore-path",
            "$.updated_at",
            cwd=workspace,
        )
        assert verify_code == 0, verify_output
        verification_files = sorted((workspace / ".assay" / "verifications").glob("verify_*.json"))
        assert len(verification_files) == 1
        diff_report = json.loads(verification_files[0].read_text(encoding="utf-8"))
        assert diff_report["total_interactions"] == 6
        assert diff_report["unchanged_count"] == 5
        assert diff_report["changed_count"] == 1
        assert diff_report["failed_count"] == 0
        assert "CHANGED" in verify_output
        changed = [delta for delta in diff_report["deltas"] if delta["classification"] == "CHANGED"]
        assert len(changed) == 1
        status_deltas = [
            delta for delta in changed[0]["body_diffs"] if delta["path"] == "$.user.profile.status"
        ]
        assert len(status_deltas) == 1
        assert (status_deltas[0]["old_value"], status_deltas[0]["new_value"]) == (
            "active",
            "pending",
        )
        diff_paths = [
            delta["path"]
            for interaction in diff_report["deltas"]
            for delta in interaction["body_diffs"] + interaction["header_diffs"]
        ]
        assert not any("transaction_id" in path or "updated_at" in path for path in diff_paths)

        # Phase 4: HTTP 5xx, abrupt disconnect, and timeout do not stop the queue.
        chaos_session_path = _write_session(
            workspace,
            "chaos-audit",
            baseline_url,
            [
                "/chaos/bad-gateway",
                "/chaos/internal",
                "/chaos/reset",
                "/chaos/hang",
                "/chaos/healthy",
            ],
        )
        chaos_code, chaos_output = await _run_cli(
            "replay",
            "--recording",
            str(chaos_session_path),
            "--target",
            candidate_url,
            "--concurrency",
            "3",
            cwd=workspace,
        )
        assert chaos_code == 0, chaos_output
        chaos_report = _load_replay_report(workspace, "chaos-audit", candidate_url)
        assert len(chaos_report["execution_pairs"]) == 5
        chaos_pairs = {pair["request"]["path"]: pair for pair in chaos_report["execution_pairs"]}
        for failed_path in (
            "/chaos/bad-gateway",
            "/chaos/internal",
            "/chaos/reset",
            "/chaos/hang",
        ):
            assert chaos_pairs[failed_path]["is_success"] is False
            assert chaos_pairs[failed_path]["error_message"]
        assert "HTTP 502" in chaos_pairs["/chaos/bad-gateway"]["error_message"]
        assert "HTTP 500" in chaos_pairs["/chaos/internal"]["error_message"]
        assert chaos_pairs["/chaos/reset"]["replayed_response"] is None
        assert chaos_pairs["/chaos/hang"]["replayed_response"] is None
        assert chaos_pairs["/chaos/healthy"]["is_success"] is True
        await asyncio.sleep(0.55)

        # Phase 5: Verify the bundle, then detect independent on-disk mutations.
        evidence_files = sorted((workspace / ".assay" / "evidence").glob("evidence_*.json"))
        assert len(evidence_files) == 1
        evidence_path = evidence_files[0]
        evidence_exit, evidence_output = await _run_cli(
            "verify-evidence",
            "--bundle",
            str(evidence_path),
            cwd=workspace,
        )
        assert evidence_exit == 0, evidence_output
        assert "EVIDENCE VERIFIED" in evidence_output
        original_evidence = json.loads(evidence_path.read_text(encoding="utf-8"))

        tamper_cases = []
        statistic_tamper = json.loads(json.dumps(original_evidence))
        assert statistic_tamper["manifest"]["summary"]["unchanged"] == 5
        statistic_tamper["manifest"]["summary"]["unchanged"] = 6
        tamper_cases.append(statistic_tamper)

        host_tamper = json.loads(json.dumps(original_evidence))
        original_host = host_tamper["manifest"]["baseline_target"]
        host_tamper["manifest"]["baseline_target"] = original_host.replace(
            "127.0.0.1",
            "127.0.0.2",
            1,
        )
        assert host_tamper["manifest"]["baseline_target"] != original_host
        tamper_cases.append(host_tamper)

        delta_tamper = json.loads(json.dumps(original_evidence))
        status_delta = next(
            field_delta
            for item in delta_tamper["deltas"]
            for field_delta in item["body_diffs"]
            if field_delta["path"] == "$.user.profile.status"
        )
        status_delta["new_value"] = "tampered"
        tamper_cases.append(delta_tamper)

        for tampered_bundle in tamper_cases:
            evidence_path.write_text(
                json.dumps(tampered_bundle, indent=2),
                encoding="utf-8",
            )
            tamper_exit, tamper_output = await _run_cli(
                "verify-evidence",
                "--bundle",
                str(evidence_path),
                cwd=workspace,
            )
            assert tamper_exit == 1, tamper_output
            assert "EVIDENCE COMPROMISED" in tamper_output
        evidence_path.write_text(
            json.dumps(original_evidence, indent=2),
            encoding="utf-8",
        )

        # Phase 6: Replay 50 interactions concurrently against both live targets.
        stress_session_path = _write_session(
            workspace,
            "stress-audit",
            baseline_url,
            [f"/stress/{index}" for index in range(50)],
        )

        async def _stress_replay(target_url: str) -> dict[str, Any]:
            exit_code, output = await _run_cli(
                "replay",
                "--recording",
                str(stress_session_path),
                "--target",
                target_url,
                "--concurrency",
                "10",
                cwd=workspace,
            )
            assert exit_code == 0, output
            return _load_replay_report(workspace, "stress-audit", target_url)

        baseline_stress, candidate_stress = await asyncio.gather(
            _stress_replay(baseline_url),
            _stress_replay(candidate_url),
        )
        for report in (baseline_stress, candidate_stress):
            assert len(report["execution_pairs"]) == 50
            assert all(pair["is_success"] for pair in report["execution_pairs"])
        assert baseline_state["active"] == 0
        assert candidate_state["active"] == 0
        assert 1 < baseline_state["max_active"] <= 10
        assert 1 < candidate_state["max_active"] <= 10
    finally:
        if recording_process is not None:
            if recording_process.returncode is None:
                recording_process.terminate()
            try:
                await asyncio.wait_for(recording_process.wait(), timeout=5)
            except asyncio.TimeoutError:
                recording_process.kill()
                await recording_process.wait()
            if recording_process.stdout is not None:
                await recording_process.stdout.read()
        for target_runner in reversed(target_runners):
            await target_runner.cleanup()

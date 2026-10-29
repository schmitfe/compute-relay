"""Bounded actual stdio-MCP acceptance; called only by the disposable local smoke."""

import asyncio
import base64
import hashlib
import json
import os
import sys
import time

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from container_smoke import Client, SmokeFailure, require


async def burst(base_url, demo_file, count, log):
    demo = json.loads(demo_file.read_text())
    require(demo["backend"] == "fake", "MCP burst requires the isolated synthetic backend")
    human = Client(base_url)
    human.login(demo["owner_username"], demo["owner_password"])
    samples, probes = [], {"/healthz": 0, f"/projects/{demo['project_id']}/": 0}
    stopped = asyncio.Event()

    async def probe():
        async with httpx.AsyncClient(base_url=base_url, timeout=3, trust_env=False,
                cookies={"sessionid": human.cookie("sessionid")}) as http:
            while not stopped.is_set():
                for path in probes:
                    started = time.monotonic()
                    response = await http.get(path)
                    require(response.status_code == 200, "Portal became unavailable during the MCP burst")
                    samples.append(time.monotonic() - started)
                    probes[path] += 1
                await asyncio.sleep(.05)

    started = time.monotonic()
    health = asyncio.create_task(probe())
    calls, busy_retries = 0, 0
    gate = asyncio.Semaphore(8)
    parameters = StdioServerParameters(command=sys.executable, args=["-m", "compute_relay.mcp"], env={
        "BROKER_CONTROL_URL": base_url, "BROKER_PROJECT_TOKEN_FILE": str(demo_file.parent / "agent-token"),
        "BROKER_EXTENSIONS": "", "PYTHONPATH": os.environ.get("PYTHONPATH", "")})
    try:
        async with asyncio.timeout(180):
            async with stdio_client(parameters, errlog=log) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def tool(name, **arguments):
                        nonlocal calls, busy_retries
                        for attempt in range(8):
                            async with gate:
                                result = await session.call_tool(name, arguments)
                            calls += 1
                            text = "".join(item.text for item in result.content if item.type == "text")
                            if not result.isError:
                                return json.loads(text)
                            if "UPLOAD_BUSY" not in text or attempt == 7:
                                raise SmokeFailure(f"MCP tool {name} failed")
                            busy_retries += 1
                            await asyncio.sleep(min(1, .05 * 2**attempt))

                    context = await tool("get_project_context")
                    require(context["project"]["id"] == demo["project_id"], "MCP selected another project")
                    descriptor = await tool("upload_job_file", path="main.py",
                        content_base64=base64.b64encode(b"raise RuntimeError('synthetic source must never execute')\n").decode())

                    async def prepare(index):
                        request = {"schema_version": "1.0", "connection_id": demo["connection_id"],
                            "name": f"mcp-burst-{index}", "command": {"interpreter": "python3", "script": "main.py", "arguments": []},
                            "environment": {"id": demo["environment_id"], "revision_sha256": demo["environment_revision_sha256"]},
                            "resources": {"nodes": 1, "cpus": 1, "gpus": 0, "memory_mib": 128, "time_limit_seconds": 60},
                            "files": [descriptor], "outputs": ["result.json"], "slurm": {"partition": "debug", "account": "broker"}}
                        key = f"mcp-burst-{index}"
                        job = await tool("prepare_job", request=request, idempotency_key=key)
                        replay = await tool("prepare_job", request=request, idempotency_key=key)
                        require(replay["id"] == job["id"], "Preparation replay created a duplicate job")
                        return job["id"]

                    jobs = await asyncio.gather(*(prepare(index) for index in range(count)))
                    require(len(set(jobs)) == count, "Burst lost a unique job")
                    executions = await asyncio.gather(*(tool("request_execution", job_id=job) for job in jobs))
                    require(len({item["attempt_id"] for item in executions}) == count, "Execution lost a unique attempt")

                    async def finish(job):
                        while True:
                            detail = await tool("get_job", job_id=job, detail=True)
                            require(len(detail["attempts"]) == 1, "Recovery created an extra compute attempt")
                            attempt = detail["attempts"][0]
                            if attempt["accounting_state"] == "settled" and attempt["collection_state"] == "complete":
                                break
                            require(attempt.get("collection_state") != "failed", "Output collection exhausted automatic recovery")
                            await asyncio.sleep(.2)
                        require(attempt["state"] == "succeeded", "A synthetic execution failed")
                        artifacts = (await tool("list_job_artifacts", attempt_id=attempt["id"]))["artifacts"]
                        require(len(artifacts) == 1, "Declared output was lost or duplicated")
                        downloaded = await tool("get_job_artifact", artifact_id=artifacts[0]["id"])
                        content = base64.b64decode(downloaded["content_base64"], validate=True)
                        require(hashlib.sha256(content).hexdigest() == artifacts[0]["sha256"], "Output checksum differs")
                        output = json.loads(content)
                        require(output["attempt_id"] == attempt["id"] and output["executed_uploaded_code"] is False,
                            "Output belongs to another attempt or executed synthetic source")
                        return attempt["native_identity"]["job_id"]

                    native_jobs = await asyncio.gather(*(finish(job) for job in jobs))
                    require(len(set(native_jobs)) == count, "Native jobs were duplicated")
                    allowance = await tool("get_project_allowance", detail=True)
                    require(allowance["spent"]["cpu_core_seconds"] == count and allowance["reserved"]["cpu_core_seconds"] == 0,
                        "Final CPU accounting does not match the synthetic allocation")
    finally:
        stopped.set()
        await health
    require(all(probes.values()), "Portal probes did not run during the burst")
    print(json.dumps({"ok": True, "backend": "fake", "simulated": True, "transport": "stdio-mcp",
        "jobs": count, "preparation_replays": count, "verified_outputs": count, "tool_calls": calls,
        "publication_busy_retries": busy_retries, "cpu_core_seconds": count, "reserved_cpu_core_seconds": 0,
        "elapsed_seconds": round(time.monotonic() - started, 3), "portal_probes": sum(probes.values()),
        "maximum_portal_latency_ms": round(max(samples) * 1000, 1)}, sort_keys=True))

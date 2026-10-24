#!/usr/bin/env python3
"""Quenched, fully asymmetric kinetic Ising verification. No job submission."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import random
import statistics
import time

from resources import effective_memory
from science import read_result, write_result


TINY = {"n": 32, "steps": 4, "replicas": 4, "disorders": 3, "seed": 19,
        "beta": 0.8, "j0": 0.6, "g": 0.5, "h": 0.15, "m0": 0.0, "dtype": "float32", "threads": 1}


def parameters(values):
    if not isinstance(values, dict) or set(values) != set(TINY):
        raise ValueError("Supply exactly the documented model parameters")
    result = dict(values)
    for key, upper in (("n", 1000000), ("steps", 1000), ("replicas", 128), ("disorders", 128), ("threads", 128)):
        if type(result[key]) is not int or not 1 <= result[key] <= upper:
            raise ValueError(f"Invalid {key}")
    if result["n"] < 2 or type(result["seed"]) is not int or not 0 <= result["seed"] <= 2**53 - 1:
        raise ValueError("Invalid size or seed")
    for key in ("beta", "j0", "g", "h", "m0"):
        if type(result[key]) not in (int, float) or not math.isfinite(result[key]) or abs(result[key]) > 10:
            raise ValueError("Model parameters must be finite and bounded")
    if result["beta"] < 0 or result["g"] < 0 or abs(result["m0"]) > 1 or result["dtype"] not in {"float32", "float64"}:
        raise ValueError("Invalid kinetic Ising parameters")
    return result


def digest(values):
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def mean_field_step(m, beta, j0, g, h, intervals=1024):
    if beta == 0:
        return 0.0
    if g == 0:
        return math.tanh(beta * (j0 * m + h))
    # Symmetric midpoint integration of a standard Gaussian on [-9,9].
    width = 18.0 / intervals
    return math.fsum(math.exp(-0.5 * z * z) * math.tanh(beta * (j0 * m + h + g * z))
                     for z in (-9 + (i + 0.5) * width for i in range(intervals))) * width / math.sqrt(2 * math.pi)


def theory(p):
    curve = [p["m0"]]
    for _ in range(p["steps"]):
        curve.append(mean_field_step(curve[-1], p["beta"], p["j0"], p["g"], p["h"]))
    return curve


def estimate(p):
    p = parameters(p)
    n, r, t, d = (p[k] for k in ("n", "replicas", "steps", "disorders"))
    matrix = n * n * (4 if p["dtype"] == "float32" else 8)
    output = 65536 + 64 * (t + 1) * d + 128 * (t + 1)
    return {"matrix_bytes": matrix, "peak_bytes": matrix + max(matrix // 4, 64 * 1024**2) + 128 * n * r + 64 * n + output,
            "matmul_flops": 2 * n * n * r * t * d, "matrix_elements_generated": n * n * d,
            "thermal_updates": n * r * t * d, "output_bound_bytes": output,
            "project_storage_bound_bytes": output + 256 * 1024 + 64 * 1024}


def preflight(p, calibration, local, grants):
    p, costs = parameters(p), estimate(p)
    if not local.get("supported") or not local.get("cgroup_resolution_complete"):
        raise ValueError("Cannot establish the effective local RAM limit")
    if costs["matrix_bytes"] <= local["effective_limit_bytes"]:
        raise ValueError("Dense matrix fits the effective local RAM limit; use a local or smaller scientific plan")
    if calibration.get("dtype") != p["dtype"] or calibration.get("threads") != p["threads"]:
        raise ValueError("Calibration dtype/CPU count must match the proposed run")
    rates = [calibration.get(k) for k in ("matrix_elements_per_second", "matmul_flops_per_second", "thermal_updates_per_second")]
    if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in rates):
        raise ValueError("Positive measured pilot throughput is required")
    expected = 30 + 4 * (costs["matrix_elements_generated"] / rates[0] + costs["matmul_flops"] / rates[1] + costs["thermal_updates"] / rates[2])
    required = {"memory_mib", "storage_available_bytes", "cpu_core_seconds_available", "wall_seconds", "maximum_flops"}
    if set(grants) != required or any(type(v) is not int or v <= 0 or v > 2**53 - 1 for v in grants.values()):
        raise ValueError("Explicit integer project resource grants are required")
    if grants["wall_seconds"] % 60:
        raise ValueError("Use whole-minute scheduler time")
    if costs["peak_bytes"] > grants["memory_mib"] * 1024**2:
        raise ValueError("Conservative dense working set exceeds the cluster memory grant")
    if costs["project_storage_bound_bytes"] > grants["storage_available_bytes"] or costs["output_bound_bytes"] > 512 * 1024:
        raise ValueError("Outputs/uploads/log allowance exceeds project storage or artifact limit")
    if costs["matmul_flops"] > min(grants["maximum_flops"], 10**15):
        raise ValueError("Scientific work exceeds the explicit operation budget")
    if expected > grants["wall_seconds"] * 0.8:
        raise ValueError("Conservative pilot extrapolation exceeds scheduler time")
    reserved = p["threads"] * grants["wall_seconds"]
    if reserved > grants["cpu_core_seconds_available"]:
        raise ValueError("Allocated CPU reservation exceeds the available project budget")
    return {"schema_version": "1.0", "accepted": True, "parameters_sha256": digest(p), "parameters": p,
            "local_memory": local, "costs": costs, "grants": grants, "calibration": calibration,
            "estimated_wall_seconds": expected, "reserved_cpu_core_seconds": reserved,
            "limitations": "Planning estimate, not an enforcement boundary; Slurm and broker policy still apply. No raw J is written."}


def _numpy(threads):
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = str(threads)
    import numpy as np
    return np


def summarize(p, curves, backend, metadata=None):
    target = theory(p)
    mean = [statistics.fmean(row[t] for row in curves) for t in range(p["steps"] + 1)]
    sem = [statistics.stdev(row[t] for row in curves) / math.sqrt(len(curves)) if len(curves) > 1 else None
           for t in range(p["steps"] + 1)]
    return {"kind": "dense-quenched-ising", "parameters": p, "parameters_sha256": digest(p), "backend": backend,
            "quenched": True, "diagonal_zero": True, "disorder_mean_curves": curves, "mean_magnetization": mean,
            "standard_error_across_disorder": sem, "mean_field": target,
            "absolute_error": [abs(a - b) for a, b in zip(mean, target)], "matrix_bytes": estimate(p)["matrix_bytes"],
            "raw_matrix_written": False, "metadata": metadata or {},
            "caveat": "Finite-size, finite-disorder transient check; disorder SEM excludes systematic finite-N and model error."}


def simulate_tiny(p=None):
    p = parameters(p or TINY)
    if p["n"] > 64 or p["steps"] > 8 or p["replicas"] > 8 or p["disorders"] > 4:
        raise ValueError("Stdlib reference is restricted to tiny verification")
    n, curves = p["n"], []
    for d in range(p["disorders"]):
        disorder = random.Random(p["seed"] + 1000003 * d)
        thermal = random.Random(p["seed"] + 2000003 * (d + 1))
        matrix = [[0.0 if i == j else disorder.gauss(p["j0"] / n, p["g"] / math.sqrt(n)) for j in range(n)] for i in range(n)]
        spins = [[1 if thermal.random() < (1 + p["m0"]) / 2 else -1 for _ in range(p["replicas"])] for _ in range(n)]
        curve = [sum(map(sum, spins)) / (n * p["replicas"])]
        for _ in range(p["steps"]):
            spins = [[1 if thermal.random() < (1 + math.tanh(p["beta"] * (p["h"] + sum(matrix[i][j] * spins[j][r] for j in range(n))))) / 2 else -1
                      for r in range(p["replicas"])] for i in range(n)]
            curve.append(sum(map(sum, spins)) / (n * p["replicas"]))
        curves.append(curve)
    return summarize(p, curves, "stdlib-float64-reference", {
        "storage_note": "Tiny reference uses Python float/list objects; matrix_bytes is the proposed NumPy numeric footprint, not Python object overhead."})


def simulate_numpy(p, *, validated_plan=None):
    p = parameters(p)
    if p["n"] > 64 or p["steps"] > 8 or p["replicas"] > 8 or p["disorders"] > 4:
        if not isinstance(validated_plan, dict) or validated_plan.get("parameters_sha256") != digest(p):
            raise ValueError("Non-tiny NumPy simulation requires an exact accepted resource plan")
        preflight(p, validated_plan["calibration"], validated_plan["local_memory"], validated_plan["grants"])
    np = _numpy(p["threads"])
    n, r, curves = p["n"], p["replicas"], []
    dtype = np.dtype(p["dtype"])
    for d in range(p["disorders"]):
        disorder = np.random.default_rng(p["seed"] + 1000003 * d)
        thermal = np.random.default_rng(p["seed"] + 2000003 * (d + 1))
        matrix = np.empty((n, n), dtype=dtype)
        disorder.standard_normal(size=matrix.shape, dtype=dtype, out=matrix)
        matrix *= p["g"] / math.sqrt(n)
        matrix += p["j0"] / n
        np.fill_diagonal(matrix, 0)
        spins = np.where(thermal.random((n, r)) < (1 + p["m0"]) / 2, 1, -1).astype(dtype)
        curve = [float(spins.mean(dtype=np.float64))]
        for _ in range(p["steps"]):
            probability = matrix @ spins
            probability += p["h"]
            probability *= p["beta"]
            np.tanh(probability, out=probability)
            probability *= 0.5
            probability += 0.5
            spins = np.where(thermal.random((n, r)) < probability, 1, -1).astype(dtype)
            curve.append(float(spins.mean(dtype=np.float64)))
        curves.append(curve)
        del matrix
    return summarize(p, curves, "numpy", {"numpy_version": np.__version__, "bit_generator": "PCG64", "threads": p["threads"]})


def calibrate(dtype="float32", threads=1):
    if dtype not in {"float32", "float64"} or type(threads) is not int or not 1 <= threads <= 8:
        raise ValueError("Invalid bounded calibration settings")
    np = _numpy(threads)
    n, r, repeats = 512, 4, 8
    rng = np.random.default_rng(41)
    matrix = np.empty((n, n), dtype=dtype)
    start = time.perf_counter()
    for _ in range(repeats):
        rng.standard_normal(size=matrix.shape, dtype=np.dtype(dtype), out=matrix)
    generation = time.perf_counter() - start
    spins = np.ones((n, r), dtype=dtype)
    matrix @ spins  # Warm up BLAS; bounded pilot only.
    start = time.perf_counter()
    for _ in range(repeats):
        fields = matrix @ spins
    multiplication = time.perf_counter() - start
    start = time.perf_counter()
    for _ in range(repeats):
        np.tanh(fields, out=fields)
        spins = np.where(rng.random((n, r)) < 0.5, 1, -1).astype(dtype)
    thermal = time.perf_counter() - start
    return {"dtype": dtype, "threads": threads, "n": n, "numpy_version": np.__version__,
            "matrix_elements_per_second": n * n * repeats / generation,
            "matmul_flops_per_second": 2 * n * n * r * repeats / multiplication,
            "thermal_updates_per_second": n * r * repeats / thermal,
            "caveat": "Small-pilot extrapolation needs a safety factor; not a runtime guarantee."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command")
    inspect = commands.add_parser("memory")
    inspect.add_argument("--output", required=True)
    pilot = commands.add_parser("calibrate")
    pilot.add_argument("--output", required=True)
    pilot.add_argument("--threads", type=int, default=1)
    pilot.add_argument("--dtype", choices=["float32", "float64"], default="float32")
    plan = commands.add_parser("preflight")
    for name in ("parameters", "calibration", "grants", "output"):
        plan.add_argument("--" + name, required=True)
    run = commands.add_parser("run")
    run.add_argument("--tiny", action="store_true")
    run.add_argument("--parameters")
    run.add_argument("--plan")
    run.add_argument("--execute-large", action="store_true")
    run.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        return
    if args.command == "memory":
        result = effective_memory()
    elif args.command == "calibrate":
        result = calibrate(args.dtype, args.threads)
    elif args.command == "preflight":
        p, _ = read_result(args.parameters)
        measured, _ = read_result(args.calibration)
        grants, _ = read_result(args.grants)
        result = preflight(p, measured, effective_memory(), grants)
    elif args.tiny:
        if args.parameters or args.plan or args.execute_large:
            raise ValueError("Tiny verification cannot be combined with a large-run plan")
        result = simulate_tiny()
    else:
        if not args.execute_large or not args.parameters or not args.plan:
            raise ValueError("A non-tiny run requires explicit parameters, accepted preflight, and --execute-large")
        p, _ = read_result(args.parameters)
        p = parameters(p)
        saved, _ = read_result(args.plan)
        if saved.get("accepted") is not True or saved.get("parameters_sha256") != digest(p):
            raise ValueError("Preflight does not bind these exact parameters")
        # Recheck arithmetic and grants from the saved local plan before any allocation.
        preflight(p, saved["calibration"], saved["local_memory"], saved["grants"])
        current = effective_memory()
        if not current.get("supported") or not current.get("cgroup_resolution_complete") or current["effective_available_bytes"] < estimate(p)["peak_bytes"]:
            raise ValueError("Compute-node effective available memory is below the conservative working set")
        result = simulate_numpy(p, validated_plan=saved)
        result["preflight_sha256"] = digest(saved)
    write_result(args.output, result)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Small, deterministic stdlib workloads. This module never submits a job."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import random
import statistics


def write_result(path, result):
    encoded = (json.dumps(result, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    if len(encoded) > 512 * 1024:
        raise ValueError("Result exceeds the acceptance kit's 512 KiB output limit")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(encoded)
    print(json.dumps({"output": str(path), "sha256": hashlib.sha256(encoded).hexdigest(), "size_bytes": len(encoded)}))


def read_result(path):
    with Path(path).open("rb") as stream:
        content = stream.read(512 * 1024 + 1)
    if len(content) > 512 * 1024:
        raise ValueError("Input exceeds 512 KiB")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    def nonfinite(value):
        raise ValueError("Nonfinite JSON value")
    return json.loads(content, object_pairs_hook=unique, parse_constant=nonfinite), hashlib.sha256(content).hexdigest()


def heat(n=128, steps=100, ratio=0.2):
    if type(n) is not int or not 8 <= n <= 4096 or type(steps) is not int or not 1 <= steps <= 10000:
        raise ValueError("Heat grid/step count exceeds the small workload bounds")
    if not 0 < ratio <= 0.5:
        raise ValueError("Explicit diffusion requires 0 < D*dt/dx^2 <= 1/2")
    values = [math.sin(2 * math.pi * i / n) for i in range(n)]
    for _ in range(steps):
        values = [values[i] + ratio * (values[(i - 1) % n] - 2 * values[i] + values[(i + 1) % n]) for i in range(n)]
    amplitude = (1 - 4 * ratio * math.sin(math.pi / n) ** 2) ** steps
    exact_discrete = [amplitude * math.sin(2 * math.pi * i / n) for i in range(n)]
    time = steps * ratio / n**2  # D=1, L=1.
    return {"kind": "periodic-heat", "n": n, "steps": steps, "ratio": ratio, "time": time,
            "discrete_amplitude": amplitude, "continuum_amplitude": math.exp(-4 * math.pi**2 * time),
            "max_discrete_error": max(abs(a - b) for a, b in zip(values, exact_discrete)),
            "spatial_mean": statistics.fmean(values), "curve": values}


def ou(samples=1000, steps=20, seed=17, theta=0.7, sigma=1.1, time=2.0):
    if not 2 <= samples <= 200000 or not 1 <= steps <= 1000 or samples * steps > 10_000_000:
        raise ValueError("OU workload exceeds 10 million scalar updates")
    if not all(math.isfinite(x) and x > 0 for x in (theta, sigma, time)):
        raise ValueError("OU parameters must be finite and positive")
    generator = random.Random(seed)
    decay = math.exp(-theta * time / steps)
    noise = sigma * math.sqrt(-math.expm1(-2 * theta * time / steps) / (2 * theta))
    sum_x = sum_x2 = sum_x4 = 0.0
    for _ in range(samples):
        x = 0.0
        for _ in range(steps):
            x = decay * x + noise * generator.gauss(0, 1)
        square = x * x
        sum_x += x
        sum_x2 += square
        sum_x4 += square * square
    return {"kind": "ou-summary", "samples": samples, "steps": steps, "seed": seed,
            "theta": theta, "sigma": sigma, "time": time, "sum_x": sum_x, "sum_x2": sum_x2,
            "sum_x4": sum_x4, "theory_second_moment": sigma**2 * -math.expm1(-2 * theta * time) / (2 * theta)}


def aggregate(paths):
    records = [read_result(path) for path in paths]
    if not records or len(records) > 32:
        raise ValueError("Aggregate between 1 and 32 independent summaries")
    first = records[0][0]
    for item, _ in records:
        if item.get("kind") != "ou-summary" or any(item.get(key) != first[key] for key in ("theta", "sigma", "time")):
            raise ValueError("OU summaries have incompatible model parameters")
    if len({item["seed"] for item, _ in records}) != len(records):
        raise ValueError("Independent replicates must use distinct seeds")
    count = sum(item["samples"] for item, _ in records)
    mean = sum(item["sum_x2"] for item, _ in records) / count
    fourth = sum(item["sum_x4"] for item, _ in records)
    variance = max(0.0, (fourth - count * mean**2) / (count - 1))
    sem = math.sqrt(variance / count)
    return {"kind": "ou-analysis", "samples": count, "replicates": len(records), "second_moment": mean,
            "standard_error": sem, "normal_approximation_95_interval": [mean - 1.96 * sem, mean + 1.96 * sem],
            "theory_second_moment": first["theory_second_moment"], "input_sha256": [digest for _, digest in records],
            "interval_caveat": "Approximate sampling interval, not a guarantee or a model-error bound."}


def generate_data(n=128, seed=31):
    if not 8 <= n <= 512:
        raise ValueError("Pipeline data is limited to 512 rows")
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        x = rng.uniform(-1, 1)
        rows.append([x, 0.4 + 1.7 * x + rng.gauss(0, 0.1)])
    return {"kind": "linear-data", "seed": seed, "rows": rows}


def fit(path):
    data, digest = read_result(path)
    if data.get("kind") != "linear-data" or not 8 <= len(data["rows"]) <= 512:
        raise ValueError("Expected bounded pipeline data")
    xs, ys = zip(*data["rows"])
    if not all(math.isfinite(v) for v in (*xs, *ys)):
        raise ValueError("Nonfinite data")
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    spread = sum((x - mx) ** 2 for x in xs)
    if spread <= 0:
        raise ValueError("Degenerate predictor")
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / spread
    return {"kind": "linear-model", "intercept": my - slope * mx, "slope": slope,
            "training_rows": len(xs), "input_sha256": digest}


def validate_model(path, seed=97):
    model, digest = read_result(path)
    if model.get("kind") != "linear-model":
        raise ValueError("Expected a fitted pipeline model")
    data = generate_data(seed=seed)
    mse = statistics.fmean((y - model["intercept"] - model["slope"] * x) ** 2 for x, y in data["rows"])
    return {"kind": "linear-validation", "model_sha256": digest, "independent_seed": seed,
            "mse": mse, "noise_variance": 0.01, "slope_error": model["slope"] - 1.7}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("smoke", "heat", "ou", "generate", "fit", "validate", "aggregate"):
        sub = commands.add_parser(name)
        sub.add_argument("--output", required=True)
        if name in {"fit", "validate"}:
            sub.add_argument("--input", required=True)
        if name == "aggregate":
            sub.add_argument("inputs", nargs="+")
        if name in {"generate", "ou"}:
            sub.add_argument("--seed", type=int, default=31 if name == "generate" else 17)
        if name == "heat":
            sub.add_argument("--n", type=int, default=128)
            sub.add_argument("--steps", type=int, default=100)
        if name == "ou":
            sub.add_argument("--samples", type=int, default=1000)
            sub.add_argument("--steps", type=int, default=20)
    args = parser.parse_args()
    if args.command == "smoke":
        result = {"kind": "stdlib-smoke", "sum_of_squares_1_to_10": sum(i * i for i in range(1, 11)), "message": "prepared bytes executed"}
    elif args.command == "heat":
        result = heat(args.n, args.steps)
    elif args.command == "ou":
        result = ou(args.samples, args.steps, args.seed)
    elif args.command == "generate":
        result = generate_data(seed=args.seed)
    elif args.command == "fit":
        result = fit(args.input)
    elif args.command == "validate":
        result = validate_model(args.input)
    else:
        result = aggregate(args.inputs)
    write_result(args.output, result)


if __name__ == "__main__":
    main()

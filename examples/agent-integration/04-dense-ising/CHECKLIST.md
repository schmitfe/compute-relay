# Dense quenched disorder and mean field acceptance

- [ ] **target_gate**: Use only assigned targets and report missing capabilities/extensions.
- [ ] **budget**: Record live allowance before/after, resource bounds, and settled zero reservations.
- [ ] **evidence**: Hash every output and identify code/config, seeds, native execution IDs and limitations.
- [ ] **theory**: Derive the disorder-averaged transient closure and state asymmetry/large-N assumptions and finite-size caveats.
- [ ] **pilot_limits**: Run tiny analytic/reproducibility tests and measured bounded NumPy calibration.
- [ ] **ram_preflight**: Record actual host+cgroup RAM; dense J alone must exceed effective local hard RAM and peak must fit cluster grant.
- [ ] **cost_preflight**: Bound matrix generation, matmul/update work, allocated CPU reservation, wall time, output/storage and environment setup.
- [ ] **quenched**: Reuse one genuinely dense Gaussian directed matrix per disorder sample over all timesteps; no artificial padding.
- [ ] **verification**: Compare transient ensemble magnetization to theory with independent-disorder uncertainty and finite-N qualification.

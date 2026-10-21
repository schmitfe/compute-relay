# Primary references and scientific scope

M. Mézard and J. Sakellariou, *Exact mean-field inference in asymmetric kinetic Ising systems*, J. Stat. Mech. (2011) L07001, [arXiv1103.3433](https://arxiv.org/abs/1103.3433), [author-hosted paper](https://www.marcmezard.fr/wp-content/uploads/2019/03/2011-Mezard-Sakellariou.pdf), equations1–5. The paper specifies synchronous binary-spin updates, independently sampled directed Gaussian couplings and no diagonal self-interactions. Its Gaussian-field result concerns the large-system limit; it does not certify this kit's finite numerical experiments.

M. Aguilera, S. A. Moosavi and H. Shimazaki, *A unifying framework for mean-field theories of asymmetric kinetic Ising systems*, Nature Communications12,1197 (2021), [primary full text](https://pmc.ncbi.nlm.nih.gov/articles/PMC7895831/), [DOI](https://doi.org/10.1038/s41467-021-20890-5). This distinguishes approximation assumptions and correlation effects. It supports treating fully asymmetric large-N theory as a scoped model, rather than transferring it to symmetric or general recurrent networks.

The case04 scalar closure is this kit's disorder-averaged transient specialization: independent off-diagonal `J_ij ~ Normal(J0/N, g²/N)`, `J_ii=0`, homogeneous field, initial spins independent of disorder, and fixed finite time as N grows. Averaging the cavity field over both disorder and thermal randomness gives mean `J0*m+h` and leading variance `g²`, hence

```text
m(t+1) = integral Dz tanh(beta * (J0*m(t) + h + g*z)).
```

This is not the single-instance local-magnetization formula with variance `sum_j J_ij²*(1-m_j²)`. Quenched row-to-row local means also fluctuate under disorder averaging. For finite N, zero diagonal introduces O(1/N) corrections, and temporal/finite-size effects need measurement. Large N followed by arbitrarily long time is not established by the finite-time argument. The kit tests transient magnetization, not equilibrium Gibbs sampling, detailed balance, critical exponents or a theorem for a fixed finite matrix.

Heat/OU/regression workloads use elementary derivations spelled out in their prompts; they are acceptance tasks rather than reproductions of a published benchmark. Gaussian quadrature uses deterministic midpoint integration on [-9,9]; compare quadrature resolutions and analytic limits before interpreting simulation error. Report independent-disorder variability separately from thermal replicates sharing the same matrix.

The [Linux kernel cgroupv2 documentation](https://docs.kernel.org/admin-guide/cgroup-v2.html) describes hierarchical `memory.max` and `memory.current`. The planner checks process membership, mount roots and ancestor limits, as well as host MemTotal/MemAvailable. Incomplete discovery blocks large-run planning. Available headroom is reported separately from the effective hard limit; the high-RAM criterion exceeds the hard limit, not merely a temporarily busy workstation's free memory.

The optional dependency is pinned to an available [NumPy2.3.4 wheel](https://pypi.org/project/numpy/2.3.4/); `wheel-source.json` records the exact filename and measured hash. This is an explicit reproducibility baseline, not a claim of the newest release or a cross-platform binary lock.

Seed reproducibility is scoped to the implementation, dtype, dependency/BLAS versions and recorded parameters. The stdlib and NumPy paths draw different random streams; NumPy float32 and float64 normal generators need not produce identical realizations. Cross-engine or cross-dtype differences are not solely floating-point rounding error.

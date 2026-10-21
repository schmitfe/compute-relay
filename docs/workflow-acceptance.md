# Disposable native workflow acceptance

`tools/workflow_smoke.py` is an opt-in HTTP client for the existing disposable Slurm fixture. It requires `--confirm-disposable-fixture` and explicit synthetic metadata identifying `slurm-fixture`, its `brokerjob` account and the fixture staging root. It refuses fake/nonfixture targets and reads no SSH key or worker credentials. The client deliberately holds the synthetic owner's session and a newly issued limited agent key only for acceptance testing.

The test creates a fresh review-only project with a three-job lifetime cap and a key containing `workflows:write` plus ordinary job/artifact scopes. It submits a three-node standard-library graph:

1. `produce` writes the exact bytes `[1,2,3]` and a newline.
2. `double` consumes that retained artifact and writes `[2,4,6]` and a newline.
3. `sum` consumes the second artifact and writes `{"sum":12}` and a newline.

Each node gets its own ordinary immutable package, human decision, native job and settled accounting. Consumers remain unmaterialized before their producer succeeds with final accounting. The client checks each exact output's SHA-256 and byte content, and verifies that the reviewed consumer package binds the producer's artifact ID, attempt ID, digest, size and destination path. Repeated explicit advance calls must keep existing node/job/execution identities. At completion every node must have exactly one execution request and one Attempt, all three submission slots must be consumed, spent CPU must be nonzero and reserved compute must be zero.

Use a fresh, explicitly named disposable Compose project. This starts no institutional cluster or real-site job:

```sh
BROKER_TEST_PORT=18768 docker compose -p cb-workflow-acceptance -f deploy/compose.test.yml -f deploy/compose.workflows-test.yml up --build -d control worker
BROKER_TEST_PORT=18768 docker compose -p cb-workflow-acceptance -f deploy/compose.test.yml -f deploy/compose.workflows-test.yml run --rm smoke
BROKER_TEST_PORT=18768 docker compose -p cb-workflow-acceptance -f deploy/compose.test.yml -f deploy/compose.workflows-test.yml down -v
```

The overlay enables only `workflows` for control/worker and mounts this client read-only, reusing the existing bounded smoke client's local-origin, CSRF and safe-error handling. On an already enabled isolated stack, run the new client with that stack's explicit synthetic demo file and no competing stack. Cleanup applies only to the named synthetic Compose project.

Default unit tests cover fixture-target refusal and tampered byte/hash/edge identity detection without contacting Docker or executing workload source. Compose configuration and unit-test success do not substitute for the native run. Retain the actual smoke JSON and final accounting result separately after execution.

# PROJECT BRIEF — Intelligent Distributed Compute Fabric for Heterogeneous Personal Devices

> Complete handover of the project vision for engineering agents and collaborators.
> This merges both team context documents without dropping content. Treat it as **vision + requirements**, not a rigid low-level spec.
> Labels used: **[REQUIRED]** fixed requirement · **[PREFERRED]** team preference, may change with justified trade-off · **[FLEXIBLE]** agent's engineering judgment · **[RISK]** known hard area.

---

## Table of contents
1. Purpose of this document
2. Project background & team
3. Name
4. Fundamental problem
5. Core definition
6. Core product idea / workflow
7. Product experience
8. Cross-platform target
9. Device pairing
10. Compute fabric & node capability model
11. Workload concept
12. Execution modes (Local · 1 Job Offloading · 2 Remote GPU · 3 Task Splitting · 4 Combined GPU)
13. Unified planning architecture
14. Execution cost model
15. Scheduler evolution & baselines
16. AI / ML role
17. Benchmarking & telemetry subsystem
18. Control plane, workers, communication, APIs, database
19. Domain objects & job lifecycle
20. Failure model, retries, idempotency
21. File/artifact transfer & caching
22. Security & isolation
23. Observability
24. UI & CLI
25. Technology preferences
26. Team / device / dev environment
27. Development philosophy & dependency order
28. Architecture principles & service boundaries
29. Documentation, roadmap, status, ADRs
30. Multi-agent development workflow
31. Code quality, testing, definition of done
32. Benchmarking discipline & research possibility
33. Fixed vs flexible
34. Existing technologies to study
35. What makes it distinctive / what it must not become
36. Major risks
37. Success levels & final vision
38. What we expect from the engineering agent
39. Appendix — Open items raised in initial architecture review
40. Decision log

---

## 1. Purpose of this document

Transfer the full context of the project to a new engineering agent or technical collaborator. It is not meant to force a specific implementation. It explains:

- what we are trying to build, and why
- how we imagine the final product
- the capabilities we want
- the technical areas involved
- how the system should behave
- the user experience we are aiming for
- our current preferred architectural ideas
- which parts are fixed and which are flexible
- where the major risks are
- what we expect from an engineering agent reviewing the project

**The agent must first understand the project completely and give an independent technical opinion. Do NOT begin implementation solely based on this document.** Analyze, challenge assumptions, identify feasibility risks, compare architectural approaches, and recommend the strongest practical engineering direction.

## 2. Project background & team

- Final Year / 7th Semester Computer Science project, team of **four students**.
- Goal: a serious, genuinely functional distributed computing platform that can be installed and tested **across real computers**.
- Ambition is intentionally high. Target the complete vision rather than prematurely removing difficult capabilities.

**The final result must NOT be reduced to:** a CRUD application · a simple website · a dashboard-only project · a chatbot · a website with an ML model / ML model connected to a frontend · a theoretical project or distributed-systems report · a simulation · a basic SSH wrapper · a toy remote-execution / distributed-computing demo · only distributed inference · only an ML scheduler · only a container orchestration demo.

**The team is willing to:** learn distributed systems, networking, GPU and CUDA concepts · study existing systems and research · inspect open-source implementations · reuse appropriate open-source components · benchmark · build prototypes · experiment · debug low-level issues · use C/C++/CUDA where needed · use Python for the majority of the platform · build incrementally and progressively improve · accept research-style experimentation for advanced features · use AI coding agents heavily.

## 3. Name

Working title: **"Intelligent Distributed Compute Fabric for Heterogeneous Personal Devices"**. Final product name undecided; "MeshForge" was used informally. The name is unimportant now; the project identity is what matters.

## 4. Fundamental problem

Personal computers normally operate as isolated compute resources. A user may own/access several trusted machines with different strengths:

| Machine | Profile |
|---|---|
| A | strong/good CPU, 16 GB RAM, no dedicated GPU |
| B | moderate CPU, NVIDIA GPU, moderate RAM |
| C | average CPU, large RAM |
| D | powerful CPU, powerful GPU |

Today, to use Machine D from Machine A the user must manually: connect (SSH/remote desktop) · transfer code · transfer datasets · install dependencies · configure the environment · launch the program · monitor it · retrieve results · clean up · repeat on other machines.

This is manual and fragmented. **We add a software layer above trusted machines so they behave like one personal compute fabric.** The user interacts with the fabric, not with individual computers.

## 5. Core definition

> An intelligent distributed computing platform that connects heterogeneous trusted personal computers, discovers their CPU/GPU/RAM/network capabilities, and automatically determines how and where a computational workload should execute.

Foundational areas: Distributed Systems · Computer Networks · Operating Systems · Scheduling · Parallel Computing · GPU Computing · Distributed AI · Machine Learning · Fault Tolerance · Security · Performance Engineering.

The system should progressively become capable of: discovering machines · understanding machine capabilities · understanding current resource usage · understanding network conditions · understanding workload requirements · generating feasible execution strategies · predicting their cost · selecting an execution plan · executing the workload · monitoring execution · handling failures where possible · returning results · storing telemetry · learning from historical executions.

## 6. Core product idea / workflow

```text
USER WORKLOAD
      |
      v
WORKLOAD / RESOURCE ANALYSIS
      |
      v
RESOURCE + NETWORK STATE
      |
      v
EXECUTION PLANNER
      |
      v
GENERATE FEASIBLE EXECUTION PLANS
      |
      v
SELECT STRATEGY
      |
      v
EXECUTE USING ONE OR MORE MACHINES
      |
      v
MONITOR EXECUTION
      |
      v
RETURN RESULT / ARTIFACTS
      |
      v
STORE TELEMETRY / PERFORMANCE DATA
      |
      v
IMPROVE FUTURE DECISIONS
```

Planner outcomes: **LOCAL · MODE 1 JOB OFFLOADING · MODE 2 REMOTE GPU · MODE 3 TASK SPLITTING · MODE 4 COMBINED GPU** (see §12). Local is the always-valid baseline.

## 7. Product experience [REQUIRED intent / FLEXIBLE details]

A real installed product — not only a browser tab. Should feel like a combination of: Docker Desktop · Tailscale-style pairing · cloud compute dashboards · local developer tooling · distributed compute systems.

The user installs software on each machine (Windows or Linux). It starts a local node agent/service which should:
- create a persistent node identity
- detect hardware: CPU, RAM, GPU, VRAM, OS details, storage, CUDA capability where relevant
- monitor resource usage
- connect securely to the compute fabric
- receive and execute workloads
- report status
- return results

The user should not need to manually configure everything every time.

## 8. Cross-platform target

- Primary OS targets: **Linux** and **Windows**.
- Development may be Linux-first; Windows machines may use WSL2 during development, but **the long-term product should ideally not require the user to understand WSL**.
- Must feel installable — not "clone a Python repo and run several shell commands".

```text
Windows:
Installer → installs node agent/service → asks for required permissions
→ starts background service → opens dashboard / local UI

Linux:
.deb / AppImage / package / installer → installs node service
→ creates configuration → starts service → dashboard / CLI available
```
Exact packaging is [FLEXIBLE].

## 9. Device pairing / joining the fabric

Imagined flow:
```text
Machine 1:  Create Fabric  →  Pairing Code: X7K9-P2LM
Machine 2:  Join Existing Fabric  →  Enter pairing code: X7K9-P2LM
```
Alternatives: invite code · QR code · account-based pairing · authenticated enrollment token · public-key confirmation · local network discovery.

**The pairing code must NOT be the permanent trust model.** A proper identity and authentication mechanism must exist underneath. Mechanism design is [FLEXIBLE] but must be done properly.

## 10. Compute fabric & node capability model

After pairing, machines are nodes of the same trusted fabric. Conceptual view:
```text
My Compute Fabric

NODE-A   Linux    CPU: Ryzen 7   RAM: 16 GB  GPU: None                    Status: Online
NODE-B   Windows  CPU: Intel i7  RAM: 32 GB  GPU: RTX 4060  VRAM: 8 GB    Status: Online
NODE-C   Windows  CPU: Ryzen 5   RAM: 16 GB  GPU: RTX 3060  VRAM: 12 GB   Status: Busy
```
The platform should continuously understand node state. Each node should eventually expose:

- **Identity:** node ID · node name · OS · software version
- **CPU:** model · core count · thread count · current utilization · available capacity
- **Memory:** total · free · used RAM
- **GPU:** presence · model · count · VRAM total · VRAM available · utilization · CUDA capability / compute capability · driver version · supported runtime info
- **Storage:** available disk · disk type · read/write performance if needed · artifact cache usage
- **Network:** latency to controller · latency to other nodes where relevant · effective bandwidth · connection stability · link quality
- **Runtime state:** active jobs · queue length · current load · cached datasets · cached models · cached execution environments · worker health · recent failures

## 11. Workload concept

A workload needs a structured description. Possible characteristics: source code · command/entrypoint · input files · output expectations · environment requirements · dependencies · datasets · models · CPU / RAM / GPU / VRAM / storage requirements · expected runtime characteristics · data-transfer requirements · communication frequency · checkpointability · workload type · resource intensity.

The abstraction must be general enough for ordinary computation **and** ML workloads. Exact job format/manifest is [FLEXIBLE].

## 12. Execution strategies

**The four execution modes (agreed with the team 2026-09-15), plus Local:**

| | **One laptop** | **Many laptops** |
|---|---|---|
| **Task is sent away** | **Mode 1 — Job Offloading**: complete task runs on another laptop; only output returns | **Mode 3 — Task Splitting**: task divided into sub-tasks; each laptop runs its sub-task; results combined |
| **Program stays on my laptop** | **Mode 2 — Remote GPU**: program on A; GPU calculations on one other laptop's GPU | **Mode 4 — Combined GPU**: program on A; all laptops' GPUs combine into one powerful GPU |

- Mode 3 is the extension of Mode 1; Mode 4 is the extension of Mode 2.
- **Local** (run on my own laptop) is always a valid baseline.
- The **AI planner** chooses among Local and Modes 1–4, which machines to use, and how much work each machine gets.
- Planned build order: Mode 1 → Mode 3 → Mode 2 → Mode 4 (planner improving throughout; final order set in the roadmap).


All strategies are part of **one unified execution system**. They must not become unrelated projects.

### 12.1 LOCAL
Workload runs entirely on the submitting machine. Important because remote is not always better — for small workloads network transfer may make remote slower. **The scheduler must always consider local execution as a valid baseline.**
```text
User submits on Machine A → Planner decides local is best → Machine A executes
```

### 12.2 MODE 1 — JOB OFFLOADING (whole-job) [REQUIRED — target: strong and highly functional]
The complete workload is transferred to one suitable remote machine, which performs the computation using its CPU/RAM/GPU.
```text
CLIENT MACHINE --(workload + files + environment info)--> REMOTE WORKER --(executes entire workload)--> RESULT/ARTIFACTS --> CLIENT
```
The platform should eventually automate:
- determining whether the workload is remotely executable
- selecting a suitable worker
- packaging workload files
- transferring source code and datasets if necessary
- transferring or resolving dependencies
- creating/reusing execution environments
- launching the workload
- monitoring execution, streaming logs, tracking resource usage
- collecting output files and exit status
- handling cancellation, timeout, worker failure
- returning artifacts · cleaning up · storing telemetry

**Not a simple SSH wrapper.** Manual SSH/file-copy already gives primitive remote execution; SSH may be used for early experiments only. The final system manages the full execution lifecycle.

**Environment management** — reproducible execution is a major challenge. Options: Docker containers · OCI images · Python virtual environments · dependency manifests · environment caching · image caching · artifact manifests. Docker is [PREFERRED] for isolation and reproducibility; exact runtime design is [FLEXIBLE]. Avoid reinstalling the same dependencies; caching should eventually be intelligent:
```text
Node B already has:
- Python environment hash A34F
- dataset hash 9D42
- model hash 22CC
Planner may prefer Node B because setup cost is lower.
```

### 12.3 MODE 2 — REMOTE GPU [REQUIRED — target: meaningful real implementation, not toy-only] [RISK]
> Program stays on Laptop A; its GPU calculations run on **one** remote GPU. Mode 4 (Combined GPU) extends this to many GPUs.
Fundamentally different from whole-job. The application stays on one machine; GPU computation physically happens on a GPU attached to another machine.
```text
CLIENT MACHINE (Application, Python, CPU, RAM)
   | tensor / operation requests
   v
NETWORK
   v
REMOTE GPU NODE (GPU Service, CUDA, NVIDIA GPU)
   v
GPU computation → result → CLIENT
```
It is **not** remote desktop, screen streaming, whole-job execution, or fake GPU simulation. It is computational remote accelerator access with **real** GPU computation on the remote machine.

Technical concerns: remote session creation · GPU sessions · GPU memory allocation/management · tensor serialization · tensor transport/transfer · remote memory handles · operation dispatch · asynchronous execution/operations · synchronization · CUDA streams · CUDA events · GPU memory lifetime · batching · caching · error propagation · GPU contention/sharing between users/jobs · multi-user access · operation compatibility · network latency (and its optimization) · bandwidth (and its optimization) · tensor size · repeated transfers · framework integration · failure handling.

**Remote GPU is not always faster.**
```text
GPU operation time: 15 ms
Network transfer:  400 ms   → remote GPU is a bad decision
```
A major scheduler responsibility is knowing **when** remote GPU is worthwhile.

Python stays primary, but C/C++/CUDA or native Python extensions are acceptable where genuinely required. Do not force pure Python where technically inappropriate.

**Transparency levels** (fully transparent remote CUDA is extremely difficult; we don't pretend it is easy):
- **Level 1** — remote execution of predefined GPU operations (matmul, convolution, PyTorch module inference)
- **Level 2** — remote tensor session; client uploads tensors / keeps server-side tensor handles; later ops refer to remote tensors
- **Level 3** — framework-level abstraction:
  ```python
  remote_device = fabric.remote_gpu("node-b")
  tensor = remote_device.tensor(...)
  result = remote_device.matmul(...)
  ```
- **Level 4** — potential PyTorch integration
- **Level 5** — advanced CUDA interception / transparent API forwarding

Determine the strongest technically meaningful level achievable. Study existing systems/research, reuse open-source work where licenses permit, support meaningful real workloads, progressively increase capability. **Do not remove it and do not fake it.**

### 12.4 MODE 3 — TASK SPLITTING [REQUIRED — target: strong and functional]
> Agreed with the team 2026-09-15. **Mode 3 is the extension of Mode 1 (Job Offloading) from one laptop to many laptops.**

The fabric **divides task A into multiple sub-tasks**, sends each sub-task to a different laptop, each laptop **runs its sub-task completely on its own GPU/CPU**, results come back to A and are **combined**.

```text
Task A (e.g. process 3000 images)
  ├── Sub-task 1 (images 1–1000)    → Laptop B runs it completely
  ├── Sub-task 2 (images 1001–2000) → Laptop C runs it completely
  └── Sub-task 3 (images 2001–3000) → Laptop D runs it completely
                     ↓
        results return to A → combined into the final result
```

- Works when the task breaks into **independent pieces**: many images/files/inputs, parameter sweeps, many experiments, dataset shards.
- The user declares how the task is divisible (manifest or SDK, e.g. "run this over each item of this dataset"); the fabric performs splitting, dispatch, collection and combining. Arbitrary programs cannot be split automatically.
- Reuses Mode 1 machinery (packaging, transfer, caching, environments, execution lifecycle, logs, artifacts) for every sub-task, and adds: split planning · per-node share sizing (stronger node → bigger share, so all finish together) · result combining · re-running a failed/slow node's sub-task elsewhere.
- Works over Wi-Fi (sub-tasks are independent; no synchronization between nodes).
- Running unrelated separate jobs on several machines is not Mode 3; Mode 3 is one task divided and recombined.

### 12.5 MODE 4 — COMBINED GPU [REQUIRED — target: meaningful real multi-GPU execution] [RISK]
> Agreed with the team 2026-09-15. **Mode 4 is the extension of Mode 2 (Remote GPU) from one GPU to many GPUs.** (Earlier name: "Combined GPU".)

The user's program **runs completely on Laptop A** — the program is **not** divided and is **not** copied to other laptops. The GPUs of all connected laptops **combine to act as one powerful GPU** for that program. Behind the scenes the fabric divides the program's **GPU calculations** across GPUs A, B, C (…), they compute in parallel, and results return to A and are combined.

```text
Program on A sees:   [ ONE GPU = A + B + C ]
Underneath:          GPU calculations divided → GPU A + GPU B + GPU C compute together → combined on A
```

**Important physical fact (agreed):** a GPU can only compute on data in its own memory, so the *calculation data* must travel to the remote GPUs over the network. What stays on A: the program, project files, environment. The user sends nothing manually; transfer of calculation data is automatic and invisible.

**Difference from Mode 3:** Mode 3 divides the **task** into complete sub-tasks that run on other laptops. Mode 4 does not divide the program; only its **GPU calculations** are spread across GPUs.
**Difference from Mode 2:** Mode 2 uses **one** remote GPU; Mode 4 combines **all** GPUs into one. Mode 4 reuses Mode 2's runtime (sessions, model/tensor transfer, result return) and adds: dividing GPU work · sizing each GPU's share · combining results · synchronizing GPUs (training) · handling slow/disconnected GPUs.

**Honest limit:** the combined power is **more than A alone but less than the full A+B+C**, because the network is ~200–500× slower than a GPU's internal link (PCIe ~16–25 GB/s vs 1 GbE ~0.11 GB/s vs Wi-Fi ~0.03–0.06 GB/s). (Historical analogy: NVIDIA SLI / AMD CrossFire combined GPUs inside *one* PC and still scaled poorly; both were discontinued.)
| Work type | Expected gain |
|---|---|
| Running an AI model on lots of data / large batches | close to A+B+C (≈70–90% of sum) |
| AI training (data-parallel, per-step sync) | good gain on Ethernet, weaker on Wi-Fi |
| One small request (one image, one chatbot reply) | none — planner uses A alone |
| Arbitrary non-AI programs | not possible |

**Scope:** PyTorch AI programs. Transparency is built up in steps: (1) fabric library call, e.g. `pool.run(model, data)` / `pool.train(model, dataset)` → (2) one-line change, e.g. `device = fabric.combined_gpu()` → (3 stretch) zero code change for PyTorch programs (e.g. `compute run --combined-gpu train.py`). Fully transparent support for *any* CUDA program (CUDA API interception, as in Juice Labs / Cricket / rCUDA) is research-level: study, don't build.

**Staged plan (each stage reuses the previous stage):**
| Stage | Capability | Difficulty | Network |
|---|---|---|---|
| 1 | Combined-GPU inference — one model over large inputs/batches across A+B+C | easiest | Wi-Fi OK |
| 2 | Combined-GPU training — data-parallel with per-step gradient sync | harder | Ethernet preferred |
| 3 (bonus) | Model too big for one GPU — model's layers split across GPUs | hardest | Ethernet |

**Heterogeneous work division (core planner/AI contribution):** machines differ significantly:
```text
Node A: RTX 4060 8 GB
Node B: RTX 3060 12 GB
Node C: CPU only, 32 GB RAM
```
The planner decides **which nodes join the pool and how much work each gets** so all parts finish at about the same time (a strong GPU gets a bigger share than a weak one). Consider: GPU speed · CPU performance · VRAM/RAM · current utilization · network bandwidth/latency per node · model size (transfer/load cost) · per-item compute cost · synchronization cost (training) · stage imbalance · straggler risk. Example:
```text
3000 images: B (strong) → 1500 · C (weaker) → 900 · A → 600  → all finish ≈ together
```
Also decide when pooling is *not* worth it (small job, slow network → fewer nodes or local).

**Stage 3 details (model splitting):**
```text
Available: Node B 8 GB VRAM, Node C 12 GB VRAM. Model: 16 GB.
layers 1-14  → Node C
layers 15-24 → Node B
```
Enables execution otherwise impossible on one device. Considerations: parameter/activation memory, per-layer compute, inter-node bandwidth, stage imbalance. Start with a controlled family of PyTorch models (sequential networks, transformers with identifiable blocks). Tensor parallelism over home networks is not practical.

Techniques overall: data parallelism · heterogeneous work sharding · gradient synchronization (all-reduce / parameter averaging) · pipeline/layer partitioning · activation transfer · memory- and network-aware placement. Use existing frameworks (e.g. PyTorch Distributed) where they work on our hardware; we need not reinvent communication primitives. **A real subset beats fake universal support.**

## 13. Unified planning architecture

Unifying question: *"Given this workload and the resources currently available, what is the best way to execute it?"* — run locally · move the entire workload · keep the app local but use a remote GPU · divide the workload/model across machines.

```text
WORKLOAD + RESOURCE STATE + NETWORK STATE
   |
   v
PLAN GENERATOR
   |
   v
FEASIBILITY FILTER
   |
   v
COST ESTIMATOR
   |
   v
POLICY / RANKING
   |
   v
SELECT EXECUTION PLAN
   |
   +-----------+------------+------------+------------+
   v           v            v            v            v
LOCAL    JOB OFFLOAD   REMOTE GPU   TASK SPLIT   COMBINED GPU
```

Example candidate generation and filtering:
```text
Plan A: Local on Node A
Plan B: Whole-job on Node B
Plan C: Whole-job on Node C       → rejected: insufficient RAM
Plan D: Remote GPU A → Node B
Plan E: Combined GPU B + C           → rejected: Node C has no compatible model runtime
```
Then estimate costs for feasible plans. The best plan abstraction is [FLEXIBLE].

## 14. Execution cost model

Plan cost components: queue_time · data_transfer_time · environment_setup_time · model_loading_time · compute_time · network_overhead · result_transfer_time · failure_risk.

```text
T_total = T_queue + T_transfer + T_environment + T_load + T_compute + T_network + T_result
```
May later become ML-driven.

## 15. Scheduler evolution & baselines

| Stage | Description |
|---|---|
| 1 Manual | user chooses node/strategy |
| 2 Simple deterministic | random, round robin, strongest suitable node, least loaded |
| 3 Resource-aware | CPU, RAM, GPU, VRAM |
| 4 State-aware | queue length, utilization, network, cached data/environments, reliability |
| 5 Performance-model based | measured formulas / historical averages |
| 6 ML-assisted | predict plan cost and/or reliability |

Initially the system works with **deterministic/rule-based scheduling**, which provides a working fallback, a baseline, a benchmark comparison target, and easier debugging. Traditional methods (random, round robin, least loaded, strongest suitable node, resource-based scoring, network-aware) **must remain available** as baselines. The system must provide measurable evidence that intelligent scheduling improves an objective: completion time · queue time · utilization · reliability · network usage.

Preferred structure (keeps explainability):
```text
Candidate Plan Generator → Feasibility Rules → Cost Estimator → Policy / Ranking → Selected Plan
Early: heuristics  →  Later: empirical cost models  →  Eventually: ML predictions
```

## 16. AI / ML role [REQUIRED: real optimization purpose]

AI is **not** a chatbot and not decorative. It is an intelligent execution planner / scheduling optimization layer that learns from real execution telemetry.

**Questions AI should eventually answer:** which execution mode/strategy? run locally? offload whole job? use remote GPU? use multiple machines? which machine? which GPU? which group of nodes forms a Combined GPU? how much work each pool node gets? how to partition a model? expected runtime? expected transfer overhead? expected queue time? failure likelihood?

**Formulation.** Direct classification (`features → LOCAL / JOB_OFFLOAD / REMOTE_GPU / TASK_SPLIT / COMBINED_GPU`) is possible, but the conceptually preferred formulation is:
```text
Generate candidate plans → Predict expected cost for each → Rank → Choose best
```
because it preserves explainability and feasibility logic. Example:
```text
Local: 52 min · Whole-job Node B: 13 min · Remote GPU Node C: 8 min · Combined GPU B+C: 11 min
Select: Remote GPU Node C
```
Evaluate this design choice; propose a stronger one if it exists.

**Input features:**
- *Workload:* workload type · input size · dataset size · file count · model size · parameter count · batch size · sequence length · CPU intensity · GPU intensity · RAM/memory requirement · VRAM requirement · compute intensity · CPU–GPU communication frequency · expected input size · expected output size · data-transfer requirements
- *Machine:* CPU model · cores · CPU availability/utilization/load · RAM total/available · GPU availability · GPU model · GPU count · GPU compute capability · GPU utilization · VRAM total/free · storage availability · storage performance/throughput
- *Network:* latency · bandwidth · packet loss · effective transfer speed · stability
- *Runtime/system state:* worker queue length · node utilization · cache state · dataset/model/environment already present · historical runtime · historical reliability · recent/previous failures

**Models:** Linear Regression · Decision Tree · Random Forest · Gradient Boosting · XGBoost · other scikit-learn models. **Do not blindly choose XGBoost — compare experimentally.** Data is largely tabular, so classical ML likely beats deep nets initially. PyTorch is mainly for actual DL workloads, Combined GPU, distributed model execution, GPU workloads.
- Regression: compute time · total completion time · transfer time · environment setup time
- Classification: failure probability · feasibility · possibly execution mode

**Long-term learned behaviour examples:**
```text
Transformer + high batch size + 10 Gbps network + GPU cached → Remote GPU performs well
small GPU workload + high latency                          → stay local
large model + insufficient single-node VRAM                → Combined GPU required
```

**Training data sources:** (1) synthetic controlled benchmarks, (2) real open-source workloads/models, (3) actual user-submitted workloads. Collect telemetry as early as practical so history exists before ML development begins.

## 17. Automated Workload Benchmarking & Telemetry System [REQUIRED — first-class, not just for the report]

Humans must not manually run thousands of jobs. The system automatically: selects/generates workloads · generates parameters · observes resource and network state · executes · tries multiple applicable strategies · collects telemetry · stores results · repeats.

```text
Generate/choose workload → Generate parameters → Observe machine/network state
→ Run with strategy 1 → Collect telemetry → Run with another applicable strategy
→ Collect telemetry → Store observations → Repeat
```

**Benchmark families:**
- *CPU-heavy:* prime calculation · matrix operations · sorting · compression · hashing · numerical/scientific computation
- *Memory-heavy:* large arrays · dataframe operations · large tensors / tensor transformations · large memory copies/transformations
- *GPU-heavy:* matrix multiplication · convolution · tensor operations · image processing
- *Real ML:* CNN inference · CNN training · transformer inference · fine-tuning · varying batch sizes, model sizes, sequence lengths
- *Communication-heavy* (important for Remote GPU and Combined GPU): repeated small tensor transfers · large activation transfers · synchronization-heavy pipelines · high-frequency remote operations

**Conditions must vary** (best strategy changes with state): GPU idle / heavily loaded · CPU idle / heavily loaded · RAM pressure · low/high latency · high/low bandwidth · cache hit/miss (dataset, model) · queue empty/busy · node failure · worker unavailable / some nodes unavailable.

**Automatic measured labels** (preferred over manual declaration):
```text
Local 72s · Whole-job 16s · Remote GPU 9s  · Combined GPU 13s → best: Remote GPU
Local 40s · Whole-job 8s  · Remote GPU 18s · Combined GPU 14s → best: Whole-job
```

**Telemetry-first.** Each execution should eventually record (schema [FLEXIBLE]): job_id · workload_id · execution_id · selected_strategy · selected_nodes · resource_snapshot · network_snapshot · queue_time · transfer_time · environment_setup_time · execution_time · result_transfer_time · total_time · success · failure_type · exit_code · CPU/RAM/GPU/VRAM utilization · cache state.

**Metrics:** queue time · file-transfer time · environment setup time · execution time · total completion time · CPU/RAM/GPU/VRAM utilization · network latency · bandwidth · success rate · failure rate · throughput · jobs per unit time. Later: energy · reliability scores · fairness.

## 18. Control plane, workers, communication, APIs, database

**Control plane.** A centralized control plane is likely the right start (easier to reason about, fits four people); it need not begin fully decentralized, but must not make future evolution impossible. Manages: node registry · node health · job submissions · scheduling · resource state · execution decisions · persistent metadata · telemetry · user-facing APIs.

**Worker agent** (on each machine): persistent node identity · registration · heartbeat · hardware detection · resource reporting · job execution · file transfer · environment management · artifact caching · logs · telemetry · cancellation · cleanup · local security boundaries.

**Internal communication [PREFERRED]:** gRPC + Protocol Buffers (strong contracts, streaming, efficient binary, good Python support, cross-language future). Possible RPCs: `RegisterNode · Heartbeat · UpdateResources · SubmitExecution · CancelExecution · StreamLogs · ReportExecutionStatus · FetchArtifact`. Exact APIs designed later.

**External APIs [PREFERRED]:** FastAPI for job submission · dashboard APIs · node info · execution history · telemetry access · scheduler decisions · authentication endpoints. No duplication: user/client-facing → FastAPI; internal node/controller → gRPC.

**Database [PREFERRED, relatively fixed]:** PostgreSQL with SQLAlchemy (or another strong PostgreSQL-compatible approach if justified). Possible entities: users · fabrics/clusters · nodes · node capabilities · node states / resource info · jobs · workload manifests · execution plans · execution attempts/executions · job status · artifacts · telemetry · benchmarks/benchmark results · failures · scheduler decisions · model versions · ML models · historical performance. Schema [FLEXIBLE].

## 19. Domain objects & job lifecycle

Conceptual (not literal class names): `Fabric · Node · NodeIdentity · ResourceSnapshot · NetworkSnapshot · Workload · Job · ExecutionPlan · ExecutionAttempt · ExecutionResult · Artifact · TelemetryRecord · SchedulerDecision · BenchmarkRun`.

Explicit job states, e.g.:
```text
SUBMITTED → ANALYZING → PLANNING → QUEUED → PREPARING → TRANSFERRING
→ STARTING → RUNNING → COLLECTING → SUCCEEDED | FAILED | CANCELLED
```
Execution attempts may need separate states. Explicit transitions matter for retries and fault tolerance.

## 20. Failure model, retries, idempotency

Distributed systems fail. Possible failures: worker disappears · controller disappears · network disconnect · timeout · partial upload · corrupted transfer · process crash · out-of-memory · GPU error · environment creation failure · unavailable dependency · disk full · stale node state · duplicate request · ambiguous retry · Combined GPU node failure.

Perfect fault tolerance is not needed immediately, but **error states must be explicit** in the architecture. Checkpointing where practical.

**Idempotency example:** client submits a job, connection drops — did the controller receive it? Blind retry may duplicate execution → request IDs / idempotency keys. (This is one reason the project is genuinely distributed-systems oriented.)

## 21. File/artifact transfer & caching

Efficiently transfer: source code · datasets · model files · configuration files · execution artifacts · logs. Options: gRPC streaming · HTTP transfer service · content-addressable artifact store · local caching · file hashing · chunking · resumable transfer. Decide after prototyping.

Caching is strategically important. Cache types: source bundle · dataset · model weights · container image · Python environment · build artifact. Content-based identity, e.g. SHA-256:
```text
model.bin  hash = 2D82...
Node B already has 2D82... → no need to transfer again
```

## 22. Security & isolation

The system allows remote code execution **by design** — security is serious. Early development may assume trusted team machines on one LAN; long-term design must consider: node identity · node enrollment · authentication · authorization · encryption · TLS · mTLS · secure pairing · secret handling · permissions · workload sandboxing · filesystem restrictions · container isolation · CPU/RAM/GPU limits · network permissions · user permissions. **Never assume arbitrary unauthenticated code execution is acceptable.**

Process isolation — workloads should not have unlimited access to the worker: Docker · Linux namespaces · cgroups · restricted user account · filesystem mount restrictions. Docker [PREFERRED] early.

## 23. Observability

Structured logging. Possible metrics: node uptime · RPC latency · queue length · active jobs · job duration · failure rate · transfer rate · CPU/RAM/GPU/VRAM utilization · scheduler decision latency. Prometheus + Grafana may come later; not required immediately.

## 24. UI & CLI

**Frontend [PREFERRED]:** React. The frontend must **not dictate or weaken the systems architecture** — the distributed backend is the core project.

**Visual style:** professional, infrastructure-focused, like Docker Desktop / GitHub / Vercel / Grafana / cloud consoles / modern dev tools. Dark mode strong default · neutral gray palette · one restrained accent · professional typography · compact readable layout · clear status indicators · information-rich panels · tables where appropriate · charts only where useful · log/terminal-style panels · minimal animation · clear hierarchy · consistent spacing.
**Avoid:** playful student-project graphics · excessive gradients · unnecessary glassmorphism · random bright colors · cartoon visuals · heavy decorative animation · obvious "AI-generated dashboard" styling.

Conceptual dashboard:
```text
┌─────────────────────────────────────────────────────────┐
│ Compute Fabric                         3 nodes online    │
├─────────────────────────────────────────────────────────┤
│ Nodes | Workloads | Combined GPU | Benchmarks | History    │
├─────────────────────────────────────────────────────────┤
│ NODE-A          NODE-B            NODE-C                │
│ Linux           Windows           Windows               │
│ ● Online        ● Online          ◐ Busy                │
│ CPU 18%         CPU 31%           CPU 74%               │
│ RAM 7/16 GB     RAM 12/32 GB      RAM 10/16 GB          │
│ GPU —           RTX 4060          RTX 3060              │
│                 GPU 12%           GPU 82%               │
├─────────────────────────────────────────────────────────┤
│ Active Workloads                                        │
│ pytorch-inference                                       │
│ Strategy: Remote GPU → NODE-B                           │
│ Status: RUNNING      Progress: 78%                      │
└─────────────────────────────────────────────────────────┘
```

Possible pages:
- **Overview:** fabric health · connected nodes · workload count · utilization · recent jobs
- **Nodes:** list · capabilities · online/offline · CPU · RAM · GPU · VRAM · network state · cached assets
- **Workloads:** submitted · queued · active · completed · failed
- **Job detail:** selected strategy · reason for selection · logs · metrics · node allocation · transfer progress · execution timeline · artifacts
- **Task Splitting:** sub-tasks · node shares · per-node progress · combined result
- **Combined GPU:** participating GPUs · work share per GPU · sync/transfer stats · throughput · model partition (if split)
- **Benchmarks:** history · comparisons · strategy performance · network conditions
- **Scheduler (later):** decision explanation · candidate plans · predicted runtime
- **Settings:** node name · fabric membership · permissions · security · resource limits

**CLI** (names undecided) — uses the same backend/control plane as the UI:
```bash
compute status
compute nodes
compute run ./project
compute jobs
compute logs <job-id>
```
An equivalent SDK/UI action is also acceptable.

**UX goal:** `compute run my_project/` and the system handles workload inspection · resource inspection · transfer planning · node selection · execution strategy · environment preparation · execution · monitoring · artifact retrieval. The user should not need to know which computer is strongest, which GPU is free, which worker has the dataset, which environment is configured, whether remote GPU is better, or whether Combined GPU is necessary.

## 25. Technology preferences [PREFERRED — not religious requirements]

| Area | Preference |
|---|---|
| Primary language | Python |
| Backend / control APIs | FastAPI |
| Internal RPC | gRPC |
| Serialization | Protocol Buffers |
| Database | PostgreSQL |
| ORM | SQLAlchemy (or strong alternative) |
| Frontend | React |
| Containers | Docker |
| System monitoring | psutil |
| NVIDIA monitoring | NVML / pynvml / nvidia-smi |
| ML | scikit-learn, XGBoost |
| Deep learning | PyTorch |
| Distributed AI | PyTorch Distributed and/or appropriate alternatives |
| GPU | CUDA |
| GPU containers | NVIDIA Container Toolkit |
| Testing | pytest |
| VCS | Git, GitHub |
| Dev environment | Linux-first; Windows + WSL2 where useful |
| Future metrics | Prometheus, Grafana |
| Security | TLS / mTLS |

If another technology is significantly better for a subsystem, **explain the trade-off before changing it**.

**Why Python:** team comfort (more than Go/Rust); integrates with PyTorch, scikit-learn, XGBoost, CUDA tooling, FastAPI, gRPC, system monitoring, benchmark analysis, data science. Python remains primary; native components (C++, CUDA, native extensions, low-level GPU components) where justified.

## 26. Team / device / dev environment

- Four team members. Roughly: **one native Linux machine, ~three Windows machines**.
- Linux machine: native Linux. Windows machines: Windows + WSL2 Ubuntu where useful.
- Standardize execution environments as much as practical.
- Initial testing on the same Wi-Fi/LAN; later Ethernet preferred for large file transfers, controlled benchmarks, Remote GPU, Combined GPU, and reproducible network measurements.
- Network characteristics should themselves be measured and included in scheduling decisions.
- **Known machine 1 (native Linux):** Intel i7-12650H (10C/16T) · 16 GB RAM · NVIDIA RTX 3050 Mobile (~4 GB VRAM) · Ubuntu 24.04 · Ethernet port available · Python 3.12, Docker 29.7, Git installed · CUDA toolkit not installed · **NVIDIA driver currently not working (nvidia-smi fails) — must be fixed before GPU work.**
- **Windows machines 2–4:** GPU/VRAM, CPU, RAM, Windows version to be confirmed by the team.
- **Wired network:** team confirmed (2026-09-15) it can get a gigabit Ethernet switch + cables (USB-to-Ethernet adapters for laptops without ports).

## 27. Development philosophy & dependency order

Incremental development, **without removing difficult long-term capabilities** just because they aren't immediately implementable.
> **Build reliable foundations first, while preserving the architecture needed for the complete vision.**
> **Do not build later layers on completely unreliable foundations.**
> Do not solve every advanced concern on day one — but do not design an architecture that makes them impossible later.

Likely dependency order (version boundaries [FLEXIBLE]; may be reorganized if technically stronger):
```text
basic repository / docs → node identity → controller-worker communication
→ resource discovery → heartbeat / health → basic remote execution
→ multi-worker scheduling → environment management → data transfer
→ artifact caching → telemetry → fault handling → security improvements
→ Task Splitting → Remote GPU → Combined GPU → performance prediction → ML scheduling
→ unified strategy optimization → productization (fault tolerance, usability,
  dashboard, APIs/SDK/CLI)
```
Telemetry should be collected throughout, not only at the end.

**First meaningful milestone** — prove real machines communicate reliably:
```text
Machine A: controller   Machine B: worker   Machine C: worker
Workers: register · report CPU/RAM/GPU · send heartbeat
         · are marked offline when disconnected · reconnect automatically
Then: A submits workload → B executes → logs stream to A → result returns
```
That is the first real compute-fabric foundation.

## 28. Architecture principles & service boundaries

- **Separate planning from execution:**
  ```text
  Workload → Planner → ExecutionPlan → Execution Backend
  Backends (illustrative): LocalExecutor · JobOffloadExecutor · TaskSplitExecutor · RemoteGPUExecutor · CombinedGPUExecutor
  ```
  The planner must not contain all execution logic.
- **No automatic microservice split** (four-person project). Separate processes only at real runtime boundaries — likely **Control Plane · Worker Agent · Frontend**. Scheduling etc. initially modular code inside the control plane.

## 29. Documentation, roadmap, status, ADRs

Strong documentation because AI coding agents are used heavily.
```text
docs/PROJECT_VISION.md (this brief: docs/PROJECT_BRIEF.md)
docs/ARCHITECTURE.md
docs/DEVELOPMENT_ROADMAP.md
docs/STATUS.md
docs/adr/
Hierarchy: PROJECT_VISION → ARCHITECTURE → ROADMAP → STATUS → IMPLEMENTATION
```

**Roadmap** (`docs/DEVELOPMENT_ROADMAP.md`, or a better structure if justified) — designed from the agent's own technical analysis and **discussed/refined with the team before major development**. Versioned `V0.1, V0.2, V0.3…`. Each version defines:
- main objective
- features/subsystems to implement
- technical concepts introduced
- dependencies required
- prerequisites (what must already work)
- how it will be tested
- exact success criteria
- working output/deliverable at the end
- what the next version builds on top of it

Break tasks down concretely — e.g. a networking version lists the actual networking tasks, not "implement networking".

Checklist:
```text
V0.1
[ ] Task 1
[ ] Task 2
V0.2
[ ] Task 1

[ ] = not started   [~] = in progress   [x] = completed
```
Mark `[x]` **only after success criteria and tests pass**, never merely because code exists. The roadmap is a **living planning/progress tool, not a rigid spec**; it always shows what is completed, in progress, remaining, blockers/problems discovered, major design changes, and the next recommended task. If development reveals the roadmap must change, update it. Before starting a new version, review the previous version's completion criteria and confirm the foundation is reliable.

**STATUS.md** — concise: Current Version · Current Task · Recently Completed · In Progress · Blockers · Known Bugs · Technical Debt · Test Status · Next Recommended Task.

**ADRs** (e.g. `0001-controller-model.md`, `0002-rpc-design.md`, `0003-artifact-transfer.md`, `0004-execution-isolation.md`) — each: context · decision · alternatives · trade-offs · consequences.

**Required workflow:**
```text
Understand complete project → Analyze technical feasibility and dependencies
→ Propose architecture → Create detailed version-by-version roadmap
→ Discuss/refine roadmap with team → Save roadmap in repository
→ Begin first version → Test it → Mark completed items → Update roadmap
→ Move to next version → Continue until the final product
```

## 30. Multi-agent development workflow

Possible roles:
- **Lead / Architect** — architecture, roadmap, interfaces, task decomposition
- **Implementer** — code for a bounded task
- **Tester** — creates tests, tries to break the implementation, verifies edge cases
- **Reviewer** — architecture compliance, code quality, concurrency, security, maintainability
- **Benchmark** — performance testing, experiment design, telemetry validation
- **Project Auditor** — roadmap alignment, architecture drift, unfinished work, technical debt, duplicate components

Loop:
```text
Lead chooses roadmap task → Architect confirms design → Implementer builds
→ Tester tests (FAIL → back to Implementer) → Reviewer checks (REJECT → back)
→ Integration tests → Acceptance criteria → Roadmap [x]
```
**An implementer never self-approves its own work.**

Use agent capabilities aggressively for architecture analysis, coding, testing, debugging, repo inspection, refactoring, documentation, benchmarking, research/codebase understanding, finding open-source libraries, and finding performance bottlenecks — but keep the codebase understandable to humans.

## 31. Code quality, testing, definition of done

**Prefer:** type hints · small modules · clear naming · explicit interfaces · structured logs · configuration · automated tests · reproducible setup · limited coupling · meaningful small commits · documented assumptions · reproducible experiments.
**Avoid:** giant files · unexplained global state · unnecessary frameworks · premature microservices · opaque AI-generated architecture · duplicated logic · an enormous codebase only the agent understands.

**Testing** — verify real behaviour. Categories: unit · API · gRPC · integration · multi-process · multi-machine · reconnection · timeout · retry · idempotency · load · benchmark · GPU · Combined GPU. Mocks are useful but must not replace real distributed tests.

**Definition of done:**
```text
[ ] implementation exists
[ ] imports/build passes
[ ] unit tests pass
[ ] integration tests pass
[ ] real machine test passes
[ ] failure behavior checked
[ ] logs verified
[ ] documentation updated
[ ] acceptance criteria satisfied
```

## 32. Benchmarking discipline & research possibility

Measurements must be reproducible. Record: hardware configuration · software version · workload parameters · network state · execution strategy · resource state · cache state · number of repetitions · timing methodology. Never conclude from a single run.

**Research (optional; main goal is a working product).** Direction: *"Adaptive workload placement across heterogeneous personal computing devices."* Question: *"When should a workload run locally, be completely offloaded, use a remote GPU, or be distributed across several machines?"*
Baselines: random · round robin · strongest machine / strongest suitable node · least loaded · resource-aware · network-aware · ML-based adaptive scheduler.
Metrics: completion time · queue time · transfer overhead · throughput · utilization · failure rate · network usage.

## 33. Fixed vs flexible

**Relatively fixed:** overall compute-fabric vision · all four execution modes (1 Job Offloading, 2 Remote GPU, 3 Task Splitting, 4 Combined GPU) are major targets · AI has a real optimization role · works on real machines · Python primary language · React frontend · PostgreSQL persistence · telemetry/benchmarking important · technically serious.

**Flexible:** exact architecture · controller design · centralized vs partially decentralized details · repository layout · database schema · RPC definitions · package choices · exact job format · job manifest · file-transfer design · caching design · scheduler formulation · ML formulation · Remote GPU implementation method · Combined GPU implementation strategy · service boundaries · implementation sequence · deployment details · testing organization · pairing mechanism · packaging.

Do not ask the team to redesign from scratch unless there is a fundamental technical flaw.

## 34. Existing technologies to study

Study for architecture lessons and reuse where appropriate — **not** to simply wrap one existing framework (no "existing product + React UI").
- **Distributed execution:** Ray · Dask · Celery · Slurm · Nomad · Kubernetes concepts
- **Remote GPU:** remote CUDA research · rCUDA · GPU virtualization research · framework-level remote execution systems
- **Distributed model execution:** PyTorch Distributed · DeepSpeed · Hugging Face Accelerate · pipeline-parallel systems · heterogeneous inference / model-partitioning systems
- **Networking:** gRPC · QUIC · HTTP/2
- **Artifact systems:** content-addressable storage · OCI registries · build caches
- **Container orchestration** systems (concepts)

Reuse rules: don't reinvent TCP, containers, cryptography, etc. **Understand licenses · understand architecture · clearly identify what we build · integrate intelligently · maintain a meaningful technical contribution.**

## 35. What makes it distinctive / what it must not become

Distinctive = the **integration** of: heterogeneous personal devices · resource awareness · multiple execution strategies · workload-aware planning · Remote GPU · collaborative model execution · telemetry · adaptive scheduling · simple user experience.

**Must not become:** simple SSH tool · Kubernetes clone · Docker UI · remote desktop product · Ray wrapper · distributed-LLM wrapper · static scheduler · benchmark dashboard · AI dashboard · chatbot · cloud-only platform · only a remote-execution utility · only an ML scheduler · only distributed inference · Combined GPU-only application. All advanced components remain integrated into one compute fabric.

## 36. Major risks

1. Remote GPU — universal transparent remote CUDA may be too large to build from scratch; explore increasingly capable subsets; never fake it.
2. Combined GPU (Mode 4) — network limits combined speed-up (never the full sum); training sync is network-sensitive; transparency for arbitrary programs is research-level. Start with inference, then data-parallel training, model splitting last.
2b. Task Splitting (Mode 3) — arbitrary programs cannot be auto-divided; divisibility must be declared; stragglers and partial failures must be handled.
3. Environment reproducibility
4. Large artifact transfer
5. Distributed state
6. Failure handling
7. Heterogeneous scheduling
8. Benchmark quality
9. ML scheduler generalization — insufficient data · measurement noise · changing hardware · leakage · overfitting · unrealistic benchmark distribution → deterministic baselines must remain available.
10. Cross-platform installation/security

Treat these honestly.

## 37. Success levels & final vision

> **Quality bar (team decision 2026-09-15):** the team has sufficient time. The goal is a **polished, finished product with actually working features** — **not** a demo or prototype. Every feature that ships must genuinely work on real machines, be tested, handle failures, and be usable without developer hand-holding. Prototypes are allowed only as internal validation steps, never as the deliverable.

| Component | Target |
|---|---|
| Mode 1 Job Offloading | strong and highly functional |
| Mode 3 Task Splitting | strong and functional; sub-tasks sized by node strength, results combined |
| Remote GPU | meaningful real implementation, not toy-only |
| Mode 4 Combined GPU | program on A uses GPUs A+B+C as one: inference + training, honest scaling measured |
| AI scheduler | genuinely integrated with actual execution telemetry |
| Platform | usable unified system |

Not every research-grade capability may reach production maturity, but the architecture must allow the complete vision to be attempted and let us **measure how far we reach**. Do not automatically reduce scope.

**Vision example 1 — Whole-job wins:**
```text
Laptop A: Linux, good CPU, 16 GB RAM, no GPU
Laptop B: Windows, RTX 4060, 32 GB RAM
Desktop C: Windows, RTX 3060, 64 GB RAM

$ compute run ./ml-project
Detected: PyTorch workload, model = 5.8 GB, input = 1.2 GB, GPU beneficial
State:  A: CPU 20%, no GPU · B: GPU 90%, 2 GB VRAM free · C: GPU 8%, 10 GB VRAM free, dataset cached
Network: A → C 2 ms, 800 Mbps
Plans:  Local 48 min · Whole-job C 8 min · Remote GPU C 11 min · Combined GPU B+C 15 min
Choose: Whole-job C (data already cached; remote GPU would add unnecessary tensor transfer)
→ runs · logs stream back · artifacts return · metrics stored · next decision uses history
```

**Example 2 — Remote GPU wins:**
```text
Local 52 min · Whole-job B 13 min · Remote GPU C 8 min · Combined GPU B+C 11 min
Selected: Remote GPU using Node C
Reason: suitable GPU · low GPU utilization · low latency · high bandwidth
        · model already cached · favorable communication pattern
```
Application on Machine A performs occasional large matrix ops; A's CPU is slow; B has a free GPU. Instead of moving the whole app, create a Remote GPU session: A keeps application logic; B holds a GPU tensor store and does CUDA execution. Large tensors upload once and stay remote; ops refer to remote tensor handles, avoiding repeated transfer.

**Example 3 — transfer dominates:**
```text
Remote GPU: compute 5 s + tensor transfer 25 s = 30 s
Whole-job:  dataset transfer 6 s + execution 10 s = 16 s  → Whole-job wins
```

**Example 4 — Task Splitting wins:** 10,000 images to classify with a script; B, C, D are free → divide into 3 sub-tasks sized by GPU strength, each laptop runs its part, results combined on A (§12.4).

**Example 4b — Combined GPU wins:** user is training a model interactively in a notebook on A → keep the program on A and let GPUs A+B+C act as one GPU (§12.5).

**Example 5 — Combined GPU (model split) required:** model needs more memory than any single available GPU → split the model across GPUs (§12.5 Stage 3).

**Final experience:** the user installs the software on trusted computers, joins them, and instead of asking *"Which machine should I use?"* asks *"Run this workload."* The system understands nodes, CPU/GPU/RAM, load, network, and workload characteristics; evaluates feasible methods; selects, executes, monitors, recovers where possible, returns results, stores telemetry, and learns from history.

**One sentence to remember:**
> Build a personal intelligent distributed compute fabric that connects heterogeneous trusted devices and automatically decides whether a workload should run locally, be completely offloaded, use a remote GPU, or be distributed across multiple machines.

## 38. What we expect from the engineering agent

**Do NOT implement first. Do NOT generate a huge codebase.** Absorb the context, then give an independent engineering assessment covering:

- **A.** Understanding of the project
- **B.** Major technical subsystems required
- **C.** Hardest engineering challenges
- **D.** Assumptions needing prototype validation
- **E.** Existing technologies/frameworks/components worth considering
- **F.** Recommended overall architecture
- **G.** Why that architecture
- **H.** Realistic development strategy that still targets the complete vision
- **I.** How Whole-Job, Remote GPU, Combined GPU and AI scheduling stay cleanly integrated
- **J.** What should be built/tested first

And evaluate:
1. Whether the overall project is technically coherent.
2. Whether Local + the four execution modes fit naturally inside one architecture.
3. Whether the product experience is realistic.
4. Whether a centralized control plane is the correct first architecture.
5. Whether Python/FastAPI/gRPC/PostgreSQL/React/Docker is a sensible core stack.
6. How Windows and Linux support should be handled.
7. How node pairing and identity should work.
8. How workload packaging should work.
9. How execution environments should be managed.
10. How large files and datasets should be transferred and cached.
11. The best abstraction for execution plans.
12. How scheduler architecture should be structured.
13. What level of Remote GPU implementation is realistic.
14. Which Remote GPU approaches should be prototyped.
15. Which existing Remote GPU systems are worth studying.
16. What Combined GPU implementation is most realistic.
17. Which distributed model frameworks should be reused.
18. How Combined GPU should support heterogeneous hardware.
19. How telemetry should be stored.
20. How automated benchmarking should work.
21. How ML scheduling should be formulated.
22. What deterministic scheduling baselines should exist.
23. How security should evolve.
24. What failure model should be designed.
25. Recommended repository structure.
26. Recommended service boundaries.
27. Recommended development roadmap.
28. Which assumptions must be validated through prototypes before committing.
29. Which parts of our current thinking you disagree with.
30. What you would change as the senior architect responsible for delivering the strongest possible version.

Be critical; don't simply agree. If something is technically weak, explain why and propose a stronger alternative. Don't remove difficult capabilities because they're difficult; don't promise impossible behaviour. We want the strongest realistic architecture that preserves the full ambition.

**Then:** create the versioned roadmap (§29), discuss and refine it with the team, save it in the repo, and proceed with implementation in small, tested, working increments.

---

## 39. Appendix — Open items raised in initial architecture review

> These came from the first review discussion. They are **proposals awaiting a team decision**, not requirements. Resolve them (ideally as ADRs) during roadmap design.

1. **Workload classes.** Local/Whole-Job are *placement* decisions for opaque jobs; Remote GPU/Combined GPU need the computation's structure (tensors/layers). A planner cannot choose Remote GPU for an arbitrary script without rewriting it. Proposal: two workload classes in one planner — **Job** (manifest + source + command → Local, Whole-Job) and **Model workload** (declared model spec + inputs, or fabric SDK → all four strategies). Enables the unified demo: one inference workload, four costed plans, explained choice.
2. **Remote GPU target.** LAN is ~200–1000× slower than PCIe, and per-op RPC round trips (0.2–1 ms Ethernet, 2–10+ ms Wi-Fi) kill naive forwarding. Proposal: resident remote sessions with tensor handles (L2) → `fabric.remote(module, node)` module offload → graph-level execution via a custom `torch.compile` backend shipping FX graphs (L4). Study rCUDA, GVirtuS, Cricket, Juice Labs; don't build L5.
3. **Combined GPU target.** *(Superseded in part by the agreed four-mode definition in §12 — see §12.5 Mode 4 stages.)* For Stage 3: pipeline inference over transformer blocks/sequential modules; no tensor parallelism over 1 GbE. Single-request pipelining gives capacity, not speed — throughput needs micro-batching. Heterogeneous partitioning via profiled per-layer time/memory + link bandwidth (minimize bottleneck stage subject to VRAM). NCCL doesn't run on native Windows → consider own activation transport over gRPC/TCP. Must study exo, Petals, llama.cpp RPC backend, distributed-llama (close overlap; differentiation = unified planner + telemetry).
4. **Windows/WSL2.** WSL2 NAT blocks inbound LAN connections by default (mirrored networking needs Windows 11 22H2+). Proposal: native Windows worker agent; workers dial out to controller over a long-lived bidirectional gRPC stream; Docker as optional isolation backend with venv fallback.
5. **Data plane.** Don't push GBs through gRPC messages; content-addressed blob store (SHA-256) over HTTP Range requests (resumable); Merkle manifest for source bundles; env hash = hash(base image + lockfile).
6. **Pairing.** Controller-generated fabric CA; join code = address + one-time token + controller cert fingerprint (or PAKE/SPAKE2 for short codes); node-generated keypair + CSR; mTLS thereafter; revocation list. Note: a worker with Docker socket access has effective root.
7. **Controller placement.** Controller runs on the "Create Fabric" machine; if it sleeps, the fabric is down — document in an ADR. (A concern was raised that PostgreSQL is heavy for an installable personal product; PostgreSQL remains the team's fixed preference — keep it, behind SQLAlchemy.)
8. **ML formulation.** Decompose cost: transfer analytically (missing bytes / measured bandwidth), env setup via cache state + history, **learn compute time** per (workload fingerprint, device). Use measured hardware scores (GFLOPS, memory bandwidth) as features rather than model names. Evaluate by **regret vs oracle**, not just MAPE; split train/test by workload to avoid leakage. Include an oracle upper bound among baselines. Benchmark sampling design needed (combinatorial explosion); network shaping via `tc netem`.
9. **Workload analyzer.** Arbitrary code cannot be reliably analyzed; use manifest declarations + static hints + history per workload fingerprint.
10. **Scope tiers & golden demo.** Tier 1: fabric, identity, Whole-Job with blob/env cache, telemetry, analytical scheduler. Tier 2: Remote GPU, Combined GPU. Tier 3: ML cost model, installers, polished UI. Define the golden demo early and build backwards.
11. **Team facts to confirm:** GPU models/VRAM per machine; Windows 10 vs 11; timeline to final evaluation; Ethernet switch availability; examiner emphasis (systems depth vs ML/research).

---

## 40. Decision log

| Date | Decision |
|---|---|
| 2026-09-15 | *(Superseded by next row.)* **ModelPool redefined as GPU pooling:** program stays on A; work is divided across A+B+C; results return to A and are combined. Stages: (1) pooled inference, (2) data-parallel pooled training, (3 bonus) model splitting across nodes. Remote GPU = single-remote-GPU case of the same runtime. Target workloads: AI training and running models (final emphasis not yet decided). |
| 2026-09-15 | Planner/AI role confirmed: decides mode (local / whole-job / remote GPU / pool), which machines, and how much work each machine gets; starts rule-based, AI later improves time predictions from recorded runs. |
| 2026-09-15 | Team will obtain a gigabit Ethernet switch and cables for pooled training, Remote GPU and controlled benchmarks. Wi-Fi remains supported for early development and pooled inference. |
| 2026-09-15 | **Final execution-mode structure:** Mode 1 Job Offloading (whole task → one laptop) · Mode 2 Remote GPU (program on A, one remote GPU) · Mode 3 Task Splitting (task divided into sub-tasks across many laptops, results combined; extends Mode 1) · Mode 4 Combined GPU (program runs entirely on A, all GPUs combine as one; extends Mode 2; calculation data must travel over the network; gain < full sum). Plus Local. "ModelPool" renamed to Combined GPU; model splitting is a Mode 4 bonus stage. |
| 2026-09-15 | **Quality bar:** team has sufficient time; deliverable is a polished, finished product with really working features, not a demo/prototype. Roadmap must include hardening, failure handling, installers/usability and full testing for each mode — not only first-working versions. |
| 2026-09-15 | **No deadline:** target is completion of the full vision at the polished quality bar, not a date. Roadmap versions are ordered by dependency, not scheduled by calendar; never cut scope for time. |
| 2026-09-15 | **All code is written by AI agents.** The team's role: product decisions, approving architecture/roadmap, running/observing tests on the physical laptops, hardware/network setup, reviewing results, and understanding the system for evaluation. |

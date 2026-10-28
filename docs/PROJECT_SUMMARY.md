# Project Summary

**Intelligent Distributed Compute Fabric for Heterogeneous Personal Devices**

## The problem

Personal computers work as isolated machines. One laptop has a good CPU but no GPU, another has a GPU, another has more RAM. To use another machine's power today, a user has to do everything manually — connect to it, copy the code and the dataset, install dependencies, set up the environment, run the program, and bring the results back. The hardware is already there, but most of it sits idle.

## Our idea

We connect a group of trusted personal computers into a single **compute fabric**. A small agent is installed on each machine and the machines are paired together. After that, the user submits a workload to the fabric instead of choosing a computer.

The system detects what each machine has (CPU, RAM, GPU, VRAM), checks how busy each machine currently is and how fast the network between them is, and then decides the best way to run that particular workload.

## What we are building

Our lab is four student laptops, and they are not equal: three have NVIDIA GPUs (two RTX 3050, one RTX 2050, about 4 GB each) and one has no GPU at all. That inequality is the point — it is what makes the decision hard, and interesting.

The system chooses between three ways of running a workload:

- **Local** — run it on the user's own machine. This is always the baseline, because for small work, sending data over the network costs more time than it saves.
- **Mode 1 — Job Offloading:** the complete task is sent to one stronger machine, runs there, and only the output comes back.
- **Mode 3 — Task Splitting:** the task is divided into smaller parts, every machine runs one part at the same time, and the results are combined.

Mode 3 is the main goal, and Mode 1 is the foundation it is built on.

## Where the intelligence is

In Mode 3 the hard question is not only *which* machines help, but **how much work each one gets**. If a slow machine is given as much as a fast one, everybody waits for the slow one to finish. A machine with no GPU should still help — just with a smaller share.

So the system learns. It records how long every real run actually took on every machine, and uses that history to predict and to divide the work better next time. This is the project's machine-learning component, and it does real optimisation work — it is not decoration.

## What version 1.0 ships

- **Local, Mode 1 Job Offloading and Mode 3 Task Splitting.** Each was tested on real laptops in the earlier lab sessions (versions 0.x); the final lab check of version 1.0 itself is still to be done.
- **The AI planner** that picks between them for every job: it starts from rules and cost estimates, corrects them with what past runs really took, and uses machine learning to predict run times. It can always say *why* it chose what it chose.
- **Security:** laptops join with a one-time code, every connection between them is encrypted and checks who is on the other end, every user logs in, and each laptop's owner decides what it shares.
- **A dashboard and a command-line tool** to see the machines, submit work, watch the logs and get the results.
- **Installers** for Windows and Linux that set up a background service and a local setup page, so joining a laptop needs no terminal commands.
- **Benchmarks and telemetry:** every real run is recorded, which is the data the planner learns from and the evaluation is measured on.

It keeps working when a laptop is switched off or disconnected in the middle of a job: its share of the work moves to the others.

## Deferred: designed, not built

Two further modes are designed and documented, and the system is built so they can be added without redesigning it. They are **not in version 1.0**, and nothing of them is faked:

- **Mode 2 — Remote GPU:** the program keeps running on the user's machine, but its GPU calculations are executed on another machine's GPU.
- **Mode 4 — Combined GPU:** the GPUs of several machines work together as one larger GPU for a single program, for AI workloads too heavy for one GPU.

Mode 4 is built on top of Mode 2, so Mode 2 is the next step. We chose to finish one mode properly rather than leave four half-working (decision record ADR-0012).

## Technical areas covered

Distributed systems · computer networks · operating systems · scheduling · parallel and GPU computing · machine learning · fault tolerance · security · performance benchmarking.

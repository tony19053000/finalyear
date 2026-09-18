# Project Summary

**Intelligent Distributed Compute Fabric for Heterogeneous Personal Devices**

## The problem

Personal computers work as isolated machines. One laptop has a good CPU but no GPU, another has a GPU, another has more RAM. To use another machine's power today, a user has to do everything manually — connect to it, copy the code and the dataset, install dependencies, set up the environment, run the program, and bring the results back. The hardware is already there, but most of it sits idle.

## Our idea

We connect a group of trusted personal computers into a single **compute fabric**. A small agent is installed on each machine and the machines are paired together. After that, the user submits a workload to the fabric instead of choosing a computer.

The system detects what each machine has (CPU, RAM, GPU, VRAM), checks how busy each machine currently is and how fast the network between them is, and then decides the best way to run that particular workload.

## How a workload can be executed

There are five possibilities, and the system chooses between them:

- **Local** — run it on the user's own machine. This is always the baseline, because for small work, sending data over the network costs more time than it saves.
- **Mode 1 — Job Offloading:** the complete task is sent to one stronger machine, runs there, and only the output comes back.
- **Mode 2 — Remote GPU:** the program keeps running on the user's machine, but its GPU calculations are executed on another machine's GPU. Useful when the user's laptop has no GPU at all.
- **Mode 3 — Task Splitting:** the task is divided into smaller parts, each machine runs one part, and the results are combined. A stronger machine gets a bigger share.
- **Mode 4 — Combined GPU:** the GPUs of several machines work together as one larger GPU for a single program, mainly for AI workloads that are too heavy for one GPU.

Mode 3 is an extension of Mode 1, and Mode 4 is an extension of Mode 2, so they are built on the same system rather than as separate tools.

## The final product

An installable application for Windows and Linux with a background service on each machine, plus a dashboard and a command-line tool. The user sees all their machines as one system, submits work, watches the logs, and receives the results — without configuring anything by hand each time.

## Technical areas covered

Distributed systems · computer networks · operating systems · scheduling · parallel and GPU computing · CUDA · distributed deep learning · machine learning · fault tolerance · security · performance benchmarking.

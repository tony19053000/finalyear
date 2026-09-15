# ADR-0003: Content-addressed data plane over HTTP

> **Status:** Accepted (2026-09-15, team approved with roadmap v1) · **Deciders:** team + architect agent

## Context
Code, datasets, models and outputs range from KB to many GB. Transfers must be resumable, verifiable, cacheable and must not be re-sent when a node already has the data.

## Decision
All bulk data are **immutable blobs named by SHA-256**, stored in CAS stores on the controller and every agent. Directories are **tree manifests** (Merkle). Clients upload only missing blobs (have/need). Nodes fetch via **HTTP range requests** from the nearest holder, in parallel chunks, verifying hashes. Agents report held blobs; the controller keeps a location index used by the planner.

## Alternatives considered
Push bytes through gRPC messages; rsync/SSH; a distributed filesystem (NFS/SMB); object store (MinIO).

## Trade-offs
+ dedup, integrity and caching for free; + resumable; + planner can price transfers exactly (`missing_bytes`). − need GC and a location index.

## Consequences
Transport choice is validated by spike S3 (V0.2); this ADR is updated with measured numbers.

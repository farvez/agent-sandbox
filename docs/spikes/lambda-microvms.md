# Spike: AWS Lambda MicroVMs as an Airlock sandbox backend

October 2026. Question: can Airlock run sessions in [Lambda MicroVMs](https://docs.aws.amazon.com/lambda/latest/dg/lambda-microvms-guide.html)
(launched June 2026) instead of, or alongside, gVisor containers on EC2, **without giving up
egress control**? Everything was tested live in the Airlock AWS account (us-east-1) with a
throwaway image and resources, all deleted afterwards.

## How it works (from the docs)

- **Images**: a zip with a Dockerfile, uploaded to S3; `create-microvm-image` builds it on an AWS
  base image (`al2023-1`), starts the app and snapshots it. MicroVMs start from that snapshot.
- **Running**: `run-microvm` gives each VM its own HTTPS endpoint
  (`<id>.lambda-microvm.<region>.on.aws`). There is **no exec API**: you run your own agent in the
  image (port 8080 by default; other ports via `X-aws-proxy-port`).
- **Auth**: every request needs a JWE token from `create-microvm-auth-token`, scoped to one VM,
  a set of ports and an expiry. HTTP/2, **WebSockets**, gRPC and SSE all work.
- **Lifecycle**: idle policy auto-suspends and auto-resumes (memory and disk kept), up to 8 hours
  total; lifecycle hooks (`/run`, `/resume`, `/suspend`, `/terminate`).
- **Network**: ingress via AWS-managed connectors (`ALL_INGRESS`, `NO_INGRESS`). Egress is open
  internet by default; a customer **VPC egress connector** routes outbound traffic through your VPC,
  where security groups and NACLs apply.
- **Limits**: ARM64 only; up to 8 GB / 4 vCPU baseline (auto-scales up to 4×); bandwidth through
  the endpoint scales with size (2 GB VM: 4 MB/s). Pricing: compute while running, snapshot storage
  while suspended (no headline rates published at launch).

## What we measured

| Test | Result |
|---|---|
| Image build (Python 3.11 + agent) | ~3 minutes |
| `run-microvm` → `RUNNING` | **4.5–4.7 s** |
| Command round trip, laptop in India → us-east-1 | first 1.5 s, then ~780 ms (mostly distance; from EC2 in-region it would be tens of ms) |
| Machine | aarch64, Debian 13, 4 vCPUs visible |
| Background process across commands | **survives** (a `sleep` started in one request was running in the next) |
| Suspend / auto-resume on next request | suspend 0.6 s; resume + command 1.3 s; files kept |
| Metadata address 169.254.169.254 | a token-protected metadata service; with no execution role it lists only `latest/ placement/ tags/` (no credentials) |
| Egress, AWS `INTERNET_EGRESS` connector | open internet |
| **Egress, no connector at all** | **still open internet** (there is no managed "no egress" connector; `NO_EGRESS` is rejected) |
| **Egress, our VPC connector** (private subnet with no internet route; SG allowing only the Airlock host on 443) | pypi.org and 1.1.1.1 **blocked**; Airlock host :443 **open**; host :22 **blocked** |
| DNS through the VPC connector | **resolves** (VPC resolver) |
| VPC connector creation | ~4 minutes (one-time per connector; reused by many VMs) |

## Conclusions

1. **Egress control is achievable**, which was the deciding question: VMs on a VPC connector into
   a route-less subnet can reach only what the security group allows, so all traffic can be forced
   through an Airlock egress proxy on our host. DNS still resolves; close that with Route 53
   Resolver DNS Firewall (deny all but the proxy's needs) before relying on it for exfiltration
   protection.
2. **What MicroVMs give that the current stack lacks**: real long-running processes, VM-level
   isolation, a WebSocket-capable endpoint per port (interactive terminal, preview URLs), suspend /
   resume with state, and capacity beyond one EC2 host.
3. **Costs and constraints**: ~4.5 s starts (vs ~1 s for a container), ARM64-only images, an
   in-region control plane is needed for low command latency, per-VM token management, unpublished
   pricing, and a larger AWS footprint (connector, subnet, roles, image pipeline).
4. **Never attach an execution role** to user VMs: anything inside could read its credentials from
   the metadata service.

## Recommendation

- Build batch 6 (interactive terminal, long-running sessions, preview URLs) **on the current gVisor
  stack first**: one long-lived container per session gives long-running processes and a PTY with
  far less new infrastructure, and keeps a single code path while there are few users.
- Add **MicroVMs as a second backend later** ("runtime: gvisor | microvm") when scale or stronger
  isolation is needed. The design that worked here: one shared VPC connector into a route-less
  subnet, an egress proxy listening on the host's private IP (per-session passes, as today), an
  agent image built from the existing sandbox image for ARM64, DNS Firewall, and the existing
  control plane (keys, limits, audit, console) unchanged.

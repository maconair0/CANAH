---
name: ip-over-optical
description: Set up IP connectivity between two sites over the optical network, building the lightpath first if there is none, then prove it with a ping. Use for "connect A and B over optical", "an IP service between sites", "IP over this lightpath", "make A reach B".
---

# IP over optical

IP between two sites rides a packet link between the routers' ports that hand off to optical transponders, and
that link rides a lightpath between those transponders. The order is fixed: the lightpath first, then the IP
service over the link, then the ping. A flow rule on one switch is not IP connectivity.

1. **Plan from what exists**: `ip_over_optical(a, z, rate_gbps)` with the sites or routers the operator named.
   It reads every controller and returns the chain (routers, handoff ports, transponders, the lightpath, the IP
   link, the IP service, a host at each end), what is missing, and the steps in order with the agent for each.
   If it says something is not published or blocked (no handoff, a transponder already carrying another
   lightpath), report exactly that and stop: do not pick other transponders or ports yourself.
2. **Make each change step with its domain's config agent**, in order, with the arguments the plan gives:
   - the lightpath with the optical config agent (it waits for approval, it is read back after);
   - then the IP service with the packet config agent, over the link the plan names.
   One step at a time: the IP link comes up only once the lightpath is in service.
3. **Prove it**: the plan's last step, `net_ping` from the host at one end to the host at the other. Report the
   ping's numbers. If it is not answered, troubleshoot (the connectivity-troubleshooting skill) before saying
   anything works.
4. **Report**: what was there, what was made (the writes as sent, from the harness's report), and the ping.
   Nothing is "set up" until the ping says so.
